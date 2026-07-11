"""
notification_service/main.py
Notification Service — FastAPI app + RabbitMQ consumer.
Runs on port 8003.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from shared.config import get_settings
from shared.db import create_tables, dispose_engine
from shared.publisher import get_publisher, close_publisher
from shared.tracing import setup_tracing
from notification_service.consumer import NotificationConsumer
from notification_service.email_builder import build_supplier_followup, build_ca_escalation
from notification_service.sender import send_email

logging.basicConfig(
    level=getattr(logging, get_settings().log_level, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("notification_service")
settings = get_settings()

_consumer: Optional[NotificationConsumer] = None
_consumer_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer, _consumer_task
    setup_tracing("notification-service")
    logger.info("Notification Service starting up...")

    if settings.is_development:
        await create_tables()

    try:
        await get_publisher()
        _consumer = NotificationConsumer()
        _consumer_task = asyncio.create_task(_consumer.start())
        logger.info("Notification consumer started")
    except Exception as e:
        logger.warning("RabbitMQ not available: %s — HTTP-only mode", e)

    yield

    if _consumer:
        await _consumer.stop()
    if _consumer_task:
        _consumer_task.cancel()
    await close_publisher()
    await dispose_engine()
    logger.info("Notification Service shut down.")


app = FastAPI(
    title="GST Reconciliation — Notification Service",
    description="Sends supplier follow-up emails and CA escalation alerts.",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "notification",
        "version": "2.0.0",
        "consumer_running": _consumer_task is not None and not _consumer_task.done(),
        "email_backend": "sendgrid" if settings.enable_sendgrid else
                         ("smtp" if settings.smtp_username else "dry-run"),
    }


class TestEmailRequest(BaseModel):
    to: str
    type: str = "supplier"  # "supplier" or "ca"


@app.post("/test-email")
async def test_email(req: TestEmailRequest):
    """
    Send a test email to verify SMTP/SendGrid configuration.
    Used during setup — not exposed in production (Phase 7).
    """
    if req.type == "supplier":
        email = build_supplier_followup(
            supplier_email=req.to,
            supplier_name="Test Supplier Pvt Ltd",
            invoice_no="TEST-INV-001",
            invoice_date="2024-03-15",
            total_amount=59000.0,
            filing_period="2024-03",
            cause_reasoning="This is a test email. Invoice appears in purchase register but not in GSTR-2A.",
            recommended_action="Please file GSTR-1 with this invoice for March 2024.",
            itc_risk=9000.0,
            ca_email=settings.email_from,
            ca_name="Test CA",
            firm_name="Test CA Firm",
        )
    else:
        email = build_ca_escalation(
            ca_email=req.to,
            ca_name="Test CA",
            client_name="Test Client Pvt Ltd",
            client_gstin="29ABCDE1234F1Z5",
            filing_period="2024-03",
            total_invoices=150,
            clean_matches=140,
            auto_count=3,
            followup_count=5,
            escalations=[{
                "invoice_no": "INV-001",
                "supplier_name": "Test Supplier",
                "gstin": "29AABCT1332L1ZT",
                "mismatch_type": "missing",
                "itc_risk": 45000.0,
                "cause_reasoning": "Invoice not found in GSTR-2A",
                "recommended_action": "Contact supplier to file GSTR-1",
            }],
            total_itc=45000.0,
        )

    success = await send_email(email)
    return {
        "sent": success,
        "to": req.to,
        "subject": email.subject,
        "backend": "sendgrid" if settings.enable_sendgrid else
                   ("smtp" if settings.smtp_username else "dry-run"),
    }
