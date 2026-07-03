"""
shared/publisher.py
RabbitMQ event publisher using aio_pika.
All services use this to publish events to the job_events topic exchange.

Exchange: job_events (topic)
Routing keys:
  - invoice.ingested
  - job.progress.{job_id}.{node}
  - mismatch.found
  - report.ready
"""

import json
import logging
from typing import Any

import aio_pika
import aio_pika.abc

from shared.config import get_settings

logger = logging.getLogger(__name__)

EXCHANGE_NAME = "job_events"


class RabbitMQPublisher:
    """
    Async RabbitMQ publisher. Creates one connection and one channel,
    reuses them across publishes. Call connect() before first publish
    and close() on shutdown.

    Usage:
        publisher = RabbitMQPublisher()
        await publisher.connect()

        await publisher.publish("invoice.ingested", {"job_id": "...", ...})

        await publisher.close()
    """

    def __init__(self, url: str | None = None):
        self._url = url or get_settings().rabbitmq_url
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None

    async def connect(self) -> None:
        """Establish connection and declare the topic exchange."""
        try:
            self._connection = await aio_pika.connect_robust(self._url)
            self._channel = await self._connection.channel()
            self._exchange = await self._channel.declare_exchange(
                EXCHANGE_NAME,
                aio_pika.ExchangeType.TOPIC,
                durable=True,
            )
            logger.info("RabbitMQ publisher connected. Exchange: %s", EXCHANGE_NAME)
        except Exception as e:
            logger.error("Failed to connect RabbitMQ publisher: %s", e)
            raise

    async def publish(self, routing_key: str, payload: dict[str, Any]) -> None:
        """
        Publish a message to the job_events exchange.

        Args:
            routing_key: e.g. "invoice.ingested", "job.progress.{job_id}.normalise"
            payload: dict that will be JSON-serialised
        """
        if not self._exchange:
            raise RuntimeError("Publisher not connected. Call connect() first.")

        body = json.dumps(payload, default=str).encode("utf-8")
        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await self._exchange.publish(message, routing_key=routing_key)
        logger.debug("Published [%s]: %s", routing_key, payload)

    async def publish_job_progress(
        self,
        job_id: str,
        node: str,
        status: str,
        progress_pct: int,
        message: str,
    ) -> None:
        """Shorthand for publishing job.progress.{job_id}.{node} events."""
        routing_key = f"job.progress.{job_id}.{node}"
        await self.publish(routing_key, {
            "job_id": job_id,
            "node": node,
            "status": status,
            "progress_pct": progress_pct,
            "message": message,
        })

    async def publish_invoice_ingested(
        self,
        job_id: str,
        client_id: str,
        filing_period: str,
        invoice_count: int,
        source: str,
    ) -> None:
        """Publish invoice.ingested event after parsing is complete."""
        await self.publish("invoice.ingested", {
            "job_id": job_id,
            "client_id": client_id,
            "filing_period": filing_period,
            "invoice_count": invoice_count,
            "source": source,
        })

    async def publish_mismatch_found(
        self,
        job_id: str,
        client_id: str,
        filing_period: str,
        mismatch_count: int,
        mismatches: list[dict],
    ) -> None:
        """Publish mismatch.found event after classification."""
        await self.publish("mismatch.found", {
            "job_id": job_id,
            "client_id": client_id,
            "filing_period": filing_period,
            "mismatch_count": mismatch_count,
            "mismatches": mismatches,
        })

    async def close(self) -> None:
        """Gracefully close channel and connection."""
        if self._channel and not self._channel.is_closed:
            await self._channel.close()
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
        logger.info("RabbitMQ publisher connection closed.")


# ── Module-level singleton for use in services ────────────
_publisher: RabbitMQPublisher | None = None


async def get_publisher() -> RabbitMQPublisher:
    """Return the module-level publisher singleton (lazy-connect)."""
    global _publisher
    if _publisher is None:
        _publisher = RabbitMQPublisher()
        await _publisher.connect()
    return _publisher


async def close_publisher() -> None:
    """Close the module-level publisher. Call on app shutdown."""
    global _publisher
    if _publisher:
        await _publisher.close()
        _publisher = None
