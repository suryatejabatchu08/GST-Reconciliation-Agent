"""
orchestration_service/consumer.py
Async RabbitMQ consumer — listens for invoice.ingested events and
triggers the reconciliation graph for each job.

Routing key consumed: "invoice.ingested"
Exchange: "job_events" (topic)
Queue: "orchestration.invoice.ingested"

After graph completes, publishes:
  - "job.progress.{job_id}.completed" with final stats
  - "mismatch.found" for each mismatch (consumed by Notification + Report services)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aio_pika
from sqlalchemy import update

from shared.config import get_settings
from shared.db import get_db_session
from shared.models import JobORM, JobStatus
from shared.publisher import get_publisher
from orchestration_service.graph import run_reconciliation

logger = logging.getLogger(__name__)
settings = get_settings()

EXCHANGE_NAME = "job_events"
QUEUE_NAME = "orchestration.invoice.ingested"
ROUTING_KEY = "invoice.ingested"


class OrchestrationConsumer:
    """
    RabbitMQ consumer that processes invoice.ingested events.
    Each message triggers one full reconciliation graph run.
    """

    def __init__(self):
        self._connection: Optional[aio_pika.abc.AbstractConnection] = None
        self._channel: Optional[aio_pika.abc.AbstractChannel] = None
        self._running = False

    async def start(self) -> None:
        """Connect to RabbitMQ and start consuming."""
        try:
            self._connection = await aio_pika.connect_robust(
                settings.rabbitmq_url,
                reconnect_interval=5,
            )
            self._channel = await self._connection.channel()
            await self._channel.set_qos(prefetch_count=1)  # Process one job at a time

            exchange = await self._channel.declare_exchange(
                EXCHANGE_NAME,
                aio_pika.ExchangeType.TOPIC,
                durable=True,
            )
            queue = await self._channel.declare_queue(
                QUEUE_NAME,
                durable=True,
                arguments={"x-message-ttl": 86400000},  # 24h TTL
            )
            await queue.bind(exchange, routing_key=ROUTING_KEY)

            self._running = True
            logger.info("Consumer listening on queue: %s", QUEUE_NAME)

            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    if not self._running:
                        break
                    async with message.process(ignore_processed=True):
                        await self._handle_message(message)

        except Exception as e:
            logger.error("Consumer failed to start: %s", e)
            raise

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._connection:
            await self._connection.close()
        logger.info("OrchestrationConsumer stopped")

    async def _handle_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        """Process a single invoice.ingested event."""
        try:
            payload = json.loads(message.body.decode())
            job_id = payload["job_id"]
            client_id = payload["client_id"]
            filing_period = payload["filing_period"]
            ca_user_id = payload.get("ca_user_id", "")

            logger.info(
                "Processing job=%s client=%s period=%s",
                job_id, client_id, filing_period
            )

            await self._run_job(job_id, client_id, filing_period, ca_user_id)

        except json.JSONDecodeError as e:
            logger.error("Invalid message JSON: %s — %s", message.body, e)
            await message.nack(requeue=False)   # Don't retry bad messages
        except KeyError as e:
            logger.error("Missing required field in message: %s", e)
            await message.nack(requeue=False)
        except Exception as e:
            logger.error("Message processing failed: %s — requeueing", e, exc_info=True)
            await message.nack(requeue=True)    # Retry transient failures

    async def _run_job(
        self,
        job_id: str,
        client_id: str,
        filing_period: str,
        ca_user_id: str,
    ) -> None:
        """Run the reconciliation graph for a job."""
        publisher = await get_publisher()

        # ── Update job status to processing ───────────────
        async with get_db_session() as db:
            await db.execute(
                update(JobORM)
                .where(JobORM.id == job_id)
                .values(
                    status=JobStatus.NORMALISING.value,
                    current_node="normalise",
                    progress_pct=10,
                )
            )
            await db.commit()

        # ── Fetch GSTIN from DB ────────────────────────────
        async with get_db_session() as db:
            from sqlalchemy import select
            from shared.models import ClientORM
            result = await db.execute(
                select(ClientORM).where(ClientORM.id == client_id)
            )
            client = result.scalar_one_or_none()
            gstin = client.gstin if client else ""

        # ── Progress callback → RabbitMQ events ───────────
        async def on_progress(node_name: str, progress_pct: int) -> None:
            """Publish job.progress event after each node completes."""
            async with get_db_session() as db:
                # Map node names to JobStatus values
                status_map = {
                    "normalise": JobStatus.NORMALISING.value,
                    "gstr2a_matcher": JobStatus.MATCHING.value,
                    "gstr1_validator": JobStatus.MATCHING.value,
                    "tax_checker": JobStatus.MATCHING.value,
                    "classifier": JobStatus.CLASSIFYING.value,
                }
                new_status = status_map.get(node_name, JobStatus.MATCHING.value)

                await db.execute(
                    update(JobORM)
                    .where(JobORM.id == job_id)
                    .values(
                        status=new_status,
                        current_node=node_name,
                        progress_pct=progress_pct,
                    )
                )
                await db.commit()

            # Publish progress event for WebSocket gateway (Phase 5)
            await publisher.publish(
                routing_key=f"job.progress.{job_id}.{node_name}",
                payload={
                    "job_id": job_id,
                    "node": node_name,
                    "progress_pct": progress_pct,
                    "status": new_status,
                }
            )

        # ── Run the graph ──────────────────────────────────
        try:
            final_state = await run_reconciliation(
                job_id=job_id,
                client_id=client_id,
                gstin=gstin,
                filing_period=filing_period,
                ca_user_id=ca_user_id,
                progress_callback=on_progress,
            )

            # ── Update job as completed ────────────────────
            async with get_db_session() as db:
                await db.execute(
                    update(JobORM)
                    .where(JobORM.id == job_id)
                    .values(
                        status=JobStatus.CLASSIFYING.value,
                        progress_pct=85,
                        total_mismatches=final_state.get("total_mismatches", 0),
                        current_node="awaiting_report",
                    )
                )
                await db.commit()

            # Publish mismatch.found events (consumed by Report + Notification services)
            for mismatch in final_state.get("classified_mismatches", []):
                await publisher.publish(
                    routing_key="mismatch.found",
                    payload={
                        "job_id": job_id,
                        "client_id": client_id,
                        "ca_user_id": ca_user_id,
                        "filing_period": filing_period,
                        "mismatch": mismatch,
                    }
                )

            # Publish completion event
            await publisher.publish(
                routing_key=f"job.progress.{job_id}.reconciliation_done",
                payload={
                    "job_id": job_id,
                    "status": "reconciliation_done",
                    "progress_pct": 85,
                    "total_mismatches": final_state.get("total_mismatches", 0),
                    "total_itc_at_risk": final_state.get("total_itc_at_risk", 0.0),
                    "auto_fixable": len(final_state.get("auto_fixable", [])),
                    "needs_followup": len(final_state.get("needs_followup", [])),
                    "needs_escalation": len(final_state.get("needs_escalation", [])),
                }
            )

            logger.info(
                "Job %s reconciliation complete: %d mismatches, ₹%.2f ITC at risk",
                job_id,
                final_state.get("total_mismatches", 0),
                final_state.get("total_itc_at_risk", 0.0),
            )

        except Exception as e:
            logger.error("Job %s graph execution failed: %s", job_id, e, exc_info=True)
            async with get_db_session() as db:
                await db.execute(
                    update(JobORM)
                    .where(JobORM.id == job_id)
                    .values(
                        status=JobStatus.FAILED.value,
                        error_message=str(e)[:500],
                        progress_pct=0,
                    )
                )
                await db.commit()
            raise
