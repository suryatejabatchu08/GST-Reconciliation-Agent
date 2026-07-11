"""
notification_service/consumer.py
RabbitMQ consumer for the Notification Service.

Listens on:
  - "mismatch.found"           → per-mismatch events from Orchestration Service

For each mismatch event:
  - severity="followup"  → send supplier follow-up email
  - severity="escalate"  → accumulate per-job, send CA escalation summary

The CA escalation is batched: all escalations for a job are collected and
sent as a single summary email when the job completes (via job.completed event).
This avoids flooding the CA with one email per mismatch.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Optional

import aio_pika

from shared.config import get_settings
from shared.db import get_db_session
from shared.models import ClientORM, JobORM
from sqlalchemy import select

from notification_service.email_builder import (
    build_supplier_followup,
    build_ca_escalation,
)
from notification_service.sender import send_email

logger = logging.getLogger(__name__)
settings = get_settings()

EXCHANGE_NAME = "job_events"
MISMATCH_QUEUE = "notification.mismatch.found"
PROGRESS_QUEUE = "notification.job.progress"


class NotificationConsumer:
    """
    Listens for mismatch.found and job.progress.*.reconciliation_done events.
    Sends supplier emails immediately on followup mismatches.
    Batches escalation mismatches per job, sends CA summary on job completion.
    """

    def __init__(self):
        self._connection = None
        self._channel = None
        self._running = False
        # Buffer: job_id → list of escalation mismatch dicts
        self._escalation_buffer: dict[str, list[dict]] = defaultdict(list)

    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(
            settings.rabbitmq_url,
            reconnect_interval=5,
        )
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=5)

        exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )

        # Queue for mismatch.found events
        mismatch_q = await self._channel.declare_queue(
            MISMATCH_QUEUE, durable=True
        )
        await mismatch_q.bind(exchange, routing_key="mismatch.found")

        # Queue for job completion events (to trigger CA escalation summary)
        progress_q = await self._channel.declare_queue(
            PROGRESS_QUEUE, durable=True
        )
        await progress_q.bind(exchange, routing_key="job.progress.#")

        self._running = True
        logger.info("NotificationConsumer started")

        # Consume both queues concurrently
        await asyncio.gather(
            self._consume(mismatch_q, self._handle_mismatch),
            self._consume(progress_q, self._handle_progress),
        )

    async def _consume(self, queue, handler) -> None:
        async with queue.iterator() as q_iter:
            async for message in q_iter:
                if not self._running:
                    break
                async with message.process(ignore_processed=True):
                    try:
                        await handler(json.loads(message.body.decode()))
                    except Exception as e:
                        logger.error("Handler error: %s", e, exc_info=True)

    async def stop(self) -> None:
        self._running = False
        if self._connection:
            await self._connection.close()

    # ── Handlers ──────────────────────────────────────────

    async def _handle_mismatch(self, payload: dict) -> None:
        """Process a single mismatch.found event."""
        mismatch = payload.get("mismatch", {})
        severity = mismatch.get("severity", "")
        job_id = payload.get("job_id", "")
        client_id = payload.get("client_id", "")
        filing_period = payload.get("filing_period", "")

        logger.info(
            "Mismatch received: job=%s severity=%s type=%s",
            job_id, severity, mismatch.get("mismatch_type")
        )

        if severity == "followup":
            await self._send_supplier_email(mismatch, filing_period, client_id, job_id)
        elif severity == "escalate":
            # Buffer for CA summary email on job completion
            self._escalation_buffer[job_id].append(mismatch)
            logger.debug("Buffered escalation for job %s (total: %d)", job_id, len(self._escalation_buffer[job_id]))

    async def _handle_progress(self, payload: dict) -> None:
        """Process job progress events — trigger CA summary on reconciliation_done."""
        job_id = payload.get("job_id", "")
        status = payload.get("status", "")

        if status == "reconciliation_done" and job_id in self._escalation_buffer:
            escalations = self._escalation_buffer.pop(job_id)
            if escalations:
                await self._send_ca_escalation_summary(
                    job_id=job_id,
                    escalations=escalations,
                    payload=payload,
                )

    async def _send_supplier_email(
        self, mismatch: dict, filing_period: str, client_id: str, job_id: str
    ) -> None:
        """Build and send a supplier follow-up email."""
        # We don't store supplier email in the mismatch record — this is a limitation.
        # In a real system, the CA would maintain a supplier contact book.
        # For now, log the follow-up action and skip sending if no email available.
        supplier_email = mismatch.get("supplier_email")
        if not supplier_email:
            logger.info(
                "No supplier email for mismatch %s/%s — logging follow-up action only",
                mismatch.get("invoice_id_books"), mismatch.get("mismatch_type")
            )
            return

        # Load CA details from DB
        ca_email, ca_name, firm_name = await self._load_ca_details(client_id)

        email = build_supplier_followup(
            supplier_email=supplier_email,
            supplier_name=mismatch.get("supplier_name", "Supplier"),
            invoice_no=mismatch.get("invoice_no", "N/A"),
            invoice_date=mismatch.get("invoice_date", ""),
            total_amount=mismatch.get("total_amount", 0.0),
            filing_period=filing_period,
            cause_reasoning=mismatch.get("cause_reasoning", ""),
            recommended_action=mismatch.get("recommended_action", ""),
            itc_risk=mismatch.get("itc_risk_amount", 0.0),
            ca_email=ca_email,
            ca_name=ca_name,
            firm_name=firm_name,
        )
        await send_email(email)

    async def _send_ca_escalation_summary(
        self, job_id: str, escalations: list[dict], payload: dict
    ) -> None:
        """Send a CA escalation summary email when a job completes."""
        client_id = payload.get("client_id", "")
        filing_period = payload.get("filing_period", "")

        ca_email, ca_name, firm_name = await self._load_ca_details(client_id)
        client_name, client_gstin = await self._load_client_details(client_id)

        total_invoices = payload.get("total_invoices", 0)
        total_mismatches = payload.get("total_mismatches", 0)
        auto_count = payload.get("auto_fixable", 0)
        followup_count = payload.get("needs_followup", 0)
        total_itc = sum(m.get("itc_risk_amount", 0) for m in escalations)

        email = build_ca_escalation(
            ca_email=ca_email,
            ca_name=ca_name,
            client_name=client_name,
            client_gstin=client_gstin,
            filing_period=filing_period,
            total_invoices=total_invoices,
            clean_matches=total_invoices - total_mismatches,
            auto_count=auto_count,
            followup_count=followup_count,
            escalations=escalations,
            total_itc=total_itc,
        )
        await send_email(email)
        logger.info(
            "CA escalation email sent: job=%s escalations=%d itc_risk=%.2f",
            job_id, len(escalations), total_itc
        )

    async def _load_ca_details(self, client_id: str) -> tuple[str, str, str]:
        """Load CA email, name, firm from DB. Returns defaults if not found."""
        try:
            async with get_db_session() as db:
                result = await db.execute(
                    select(ClientORM).where(ClientORM.id == client_id)
                )
                client = result.scalar_one_or_none()
                if client:
                    return (
                        settings.email_from,   # CA email from config (Phase 7: from auth)
                        "CA",
                        client.firm_name or "Your Firm",
                    )
        except Exception as e:
            logger.warning("Could not load CA details: %s", e)
        return settings.email_from, "CA", "Your Firm"

    async def _load_client_details(self, client_id: str) -> tuple[str, str]:
        """Load client name and GSTIN from DB."""
        try:
            async with get_db_session() as db:
                result = await db.execute(
                    select(ClientORM).where(ClientORM.id == client_id)
                )
                client = result.scalar_one_or_none()
                if client:
                    return client.firm_name or "Client", client.gstin
        except Exception as e:
            logger.warning("Could not load client details: %s", e)
        return "Client", "N/A"
