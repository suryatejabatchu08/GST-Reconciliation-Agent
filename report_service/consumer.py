"""
report_service/consumer.py
RabbitMQ consumer for the Report Service.

Listens on:
  - "job.progress.#"   → watches for reconciliation_done status

On reconciliation_done:
  1. Loads all classified mismatches from DB for this job
  2. Generates PDF + Excel reports
  3. Saves to ./reports/{job_id}/ directory
  4. Publishes report.ready event with file paths
  5. Updates Job record with report_url
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import aio_pika
from sqlalchemy import select, update

from shared.config import get_settings
from shared.db import get_db_session
from shared.models import JobORM, ClientORM, MismatchORM, JobStatus
from shared.publisher import get_publisher

from report_service.pdf_builder import build_pdf_report
from report_service.excel_builder import build_excel_report

logger = logging.getLogger(__name__)
settings = get_settings()

EXCHANGE_NAME = "job_events"
QUEUE_NAME = "report.job.progress"
REPORTS_DIR = Path("reports")


class ReportConsumer:
    """Generates PDF + Excel reports when a reconciliation job completes."""

    def __init__(self):
        self._connection = None
        self._channel = None
        self._running = False

    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(
            settings.rabbitmq_url, reconnect_interval=5
        )
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=1)

        exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )
        queue = await self._channel.declare_queue(QUEUE_NAME, durable=True)
        await queue.bind(exchange, routing_key="job.progress.#")

        self._running = True
        logger.info("ReportConsumer started, listening on %s", QUEUE_NAME)

        async with queue.iterator() as q_iter:
            async for message in q_iter:
                if not self._running:
                    break
                async with message.process(ignore_processed=True):
                    try:
                        payload = json.loads(message.body.decode())
                        if payload.get("status") == "reconciliation_done":
                            await self._generate_reports(payload)
                    except Exception as e:
                        logger.error("Report generation failed: %s", e, exc_info=True)

    async def stop(self) -> None:
        self._running = False
        if self._connection:
            await self._connection.close()

    async def _generate_reports(self, payload: dict) -> None:
        """Load mismatches from DB and generate PDF + Excel reports."""
        job_id = payload.get("job_id", "")
        client_id = payload.get("client_id", "")
        filing_period = payload.get("filing_period", "")

        logger.info("Generating reports for job=%s", job_id)

        # ── Load data from DB ──────────────────────────────
        client_name, client_gstin, ca_name, firm_name = await self._load_client_info(client_id)
        auto_ms, followup_ms, escalation_ms = await self._load_mismatches(job_id)

        total_invoices = payload.get("total_invoices", 0)
        matched = total_invoices - (len(auto_ms) + len(followup_ms) + len(escalation_ms))
        total_itc = sum(m.get("itc_risk_amount", 0) for m in escalation_ms + followup_ms)

        report_args = dict(
            client_name=client_name,
            client_gstin=client_gstin,
            filing_period=filing_period,
            ca_name=ca_name,
            firm_name=firm_name,
            total_invoices=total_invoices,
            matched_count=max(0, matched),
            auto_mismatches=auto_ms,
            followup_mismatches=followup_ms,
            escalation_mismatches=escalation_ms,
            total_itc_at_risk=total_itc,
        )

        # ── Generate files ─────────────────────────────────
        job_dir = REPORTS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        pdf_path = job_dir / f"reconciliation_{filing_period}_{timestamp}.pdf"
        excel_path = job_dir / f"reconciliation_{filing_period}_{timestamp}.xlsx"

        try:
            # PDF generation (run in executor — ReportLab is synchronous)
            pdf_bytes = await asyncio.get_event_loop().run_in_executor(
                None, lambda: build_pdf_report(**report_args)
            )
            pdf_path.write_bytes(pdf_bytes)
            logger.info("PDF saved: %s (%d KB)", pdf_path, len(pdf_bytes) // 1024)
        except Exception as e:
            logger.error("PDF generation failed: %s", e)
            pdf_path = None

        try:
            # Excel generation
            xlsx_bytes = await asyncio.get_event_loop().run_in_executor(
                None, lambda: build_excel_report(**report_args)
            )
            excel_path.write_bytes(xlsx_bytes)
            logger.info("Excel saved: %s (%d KB)", excel_path, len(xlsx_bytes) // 1024)
        except Exception as e:
            logger.error("Excel generation failed: %s", e)
            excel_path = None

        # ── Update Job record with report URL ──────────────
        pdf_url = str(pdf_path) if pdf_path else None
        async with get_db_session() as db:
            await db.execute(
                update(JobORM)
                .where(JobORM.id == job_id)
                .values(
                    status=JobStatus.DONE.value,
                    progress_pct=100,
                    report_url=pdf_url,
                    completed_at=datetime.utcnow(),
                    total_mismatches=len(auto_ms) + len(followup_ms) + len(escalation_ms),
                )
            )
            await db.commit()

        # ── Publish report.ready ───────────────────────────
        try:
            publisher = await get_publisher()
            await publisher.publish("report.ready", {
                "job_id": job_id,
                "client_id": client_id,
                "filing_period": filing_period,
                "pdf_path": str(pdf_path) if pdf_path else None,
                "excel_path": str(excel_path) if excel_path else None,
                "total_mismatches": len(auto_ms) + len(followup_ms) + len(escalation_ms),
                "total_itc_at_risk": total_itc,
            })
            logger.info("report.ready published for job=%s", job_id)
        except Exception as e:
            logger.warning("Failed to publish report.ready: %s", e)

    async def _load_client_info(self, client_id: str) -> tuple[str, str, str, str]:
        try:
            async with get_db_session() as db:
                result = await db.execute(
                    select(ClientORM).where(ClientORM.id == client_id)
                )
                client = result.scalar_one_or_none()
                if client:
                    return (
                        client.firm_name or "Client",
                        client.gstin,
                        settings.email_from_name or "CA",
                        settings.email_from_name or "CA Firm",
                    )
        except Exception as e:
            logger.warning("Could not load client info: %s", e)
        return "Client", "N/A", "CA", "CA Firm"

    async def _load_mismatches(
        self, job_id: str
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Load classified mismatches from DB, split by severity."""
        auto_ms, followup_ms, escalation_ms = [], [], []
        try:
            async with get_db_session() as db:
                result = await db.execute(
                    select(MismatchORM).where(MismatchORM.job_id == job_id)
                )
                mismatches = result.scalars().all()

            for m in mismatches:
                record = {
                    "invoice_no": None,   # Not stored in MismatchORM directly
                    "supplier_name": None,
                    "gstin": None,
                    "mismatch_type": m.mismatch_type,
                    "severity": m.severity,
                    "itc_risk_amount": float(m.itc_risk_amount or 0),
                    "cause_reasoning": m.cause_reasoning or "",
                    "recommended_action": "",
                }
                if m.severity == "auto":
                    auto_ms.append(record)
                elif m.severity == "followup":
                    followup_ms.append(record)
                else:
                    escalation_ms.append(record)

        except Exception as e:
            logger.error("Could not load mismatches from DB: %s", e)

        return auto_ms, followup_ms, escalation_ms
