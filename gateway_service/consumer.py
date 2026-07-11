"""
gateway_service/consumer.py
RabbitMQ consumer for the Gateway Service.

Subscribes to job.progress.# events and broadcasts them to
the correct WebSocket clients via the WebSocketManager.

This is the bridge between the backend event system (RabbitMQ)
and the frontend real-time UI (WebSocket).

Message flow:
  Orchestration Service publishes → job.progress.{job_id}.{node}
  Gateway consumer receives it
  Gateway looks up WebSocket connections for job_id
  Gateway broadcasts the progress payload to all connected browser tabs
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import aio_pika

from shared.config import get_settings
from gateway_service.websocket_manager import get_ws_manager

logger = logging.getLogger(__name__)
settings = get_settings()

EXCHANGE_NAME = "job_events"
QUEUE_NAME = "gateway.job.progress"


class GatewayConsumer:
    """
    Subscribes to all job.progress.# and report.ready events.
    Forwards each event to the WebSocketManager for broadcast.
    """

    def __init__(self):
        self._connection = None
        self._channel = None
        self._running = False

    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(
            settings.rabbitmq_url,
            reconnect_interval=5,
        )
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=20)  # Progress events are tiny, allow more

        exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )

        queue = await self._channel.declare_queue(
            QUEUE_NAME,
            durable=False,         # Non-durable: progress events are ephemeral
            auto_delete=True,      # Delete queue when consumer disconnects
            arguments={"x-message-ttl": 30000},  # 30s TTL — stale progress events useless
        )

        # Subscribe to progress AND report events
        await queue.bind(exchange, routing_key="job.progress.#")
        await queue.bind(exchange, routing_key="report.ready")

        self._running = True
        logger.info("Gateway consumer started — listening for progress events")

        async with queue.iterator() as q_iter:
            async for message in q_iter:
                if not self._running:
                    break
                async with message.process():
                    await self._handle(message)

    async def stop(self) -> None:
        self._running = False
        if self._connection:
            await self._connection.close()

    async def _handle(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        """Route each RabbitMQ event to the appropriate WebSocket clients."""
        try:
            payload = json.loads(message.body.decode())
            job_id = payload.get("job_id")

            if not job_id:
                return

            manager = get_ws_manager()

            # Enrich payload with event type
            routing_key = message.routing_key or ""
            if routing_key == "report.ready":
                payload["type"] = "report_ready"
                payload["message"] = "Your reconciliation report is ready to download."
            else:
                # job.progress.{job_id}.{node}
                parts = routing_key.split(".")
                node = parts[-1] if len(parts) >= 4 else "unknown"
                payload["type"] = "progress"
                payload.setdefault("node", node)
                payload.setdefault("message", f"Processing: {node.replace('_', ' ').title()}")

            await manager.broadcast(job_id, payload)
            logger.debug("Broadcast to job=%s: %s", job_id, payload.get("type"))

        except json.JSONDecodeError:
            logger.error("Invalid JSON in progress event: %s", message.body[:100])
        except Exception as e:
            logger.error("Gateway consumer handler error: %s", e, exc_info=True)
