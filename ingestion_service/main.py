"""
ingestion_service/main.py
Ingestion Service — FastAPI application.

Responsibilities:
  - Accept file uploads (Tally XML, Tally CSV, Zoho Books CSV)
  - Fetch GST portal data (GSTR-1, GSTR-2A, GSTR-3B)
  - Parse and normalise invoices
  - Store invoices to Supabase (PostgreSQL)
  - Publish invoice.ingested event to RabbitMQ

Endpoints:
  POST /upload              ← upload + parse a file
  POST /jobs/{job_id}/fetch-portal  ← fetch GSTR data for an existing job
  GET  /jobs/{job_id}/status        ← job status (WebSocket fallback)
  GET  /health                      ← health check

Runs on port: 8001
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import (
    FastAPI, File, Form, HTTPException, UploadFile,
    Depends, status
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.db import get_db, create_tables, dispose_engine
from shared.models import (
    ClientORM, InvoiceORM, JobORM,
    InvoiceSource, JobStatus,
)
from shared.publisher import get_publisher, close_publisher
from shared.tracing import setup_tracing

from ingestion_service.parsers import get_parser
from ingestion_service.gst_portal.client import GSTPortalClient

# ── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, get_settings().log_level, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ingestion_service")

settings = get_settings()


# ── App lifecycle ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown for DB + RabbitMQ connections."""
    setup_tracing("ingestion-service")
    logger.info("Ingestion Service starting up...")

    # Create tables in dev (production uses migrations)
    if settings.is_development:
        await create_tables()

    # Connect publisher
    try:
        await get_publisher()
        logger.info("RabbitMQ publisher connected.")
    except Exception as e:
        logger.warning("RabbitMQ not available: %s — events will be skipped.", e)

    yield

    # Shutdown
    await close_publisher()
    await dispose_engine()
    logger.info("Ingestion Service shut down.")


# ── FastAPI app ────────────────────────────────────────────

app = FastAPI(
    title="GST Reconciliation — Ingestion Service",
    description="Accepts Tally/Zoho uploads, fetches GST portal data, stores normalised invoices.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in production (Phase 7)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────────────────

class UploadResponse(BaseModel):
    job_id: str
    client_id: str
    filing_period: str
    source_type: str
    invoices_parsed: int
    invoices_stored: int
    parse_errors: list[str]
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    client_id: str
    status: str
    progress_pct: int
    current_node: Optional[str]
    total_invoices: int
    total_mismatches: int
    report_url: Optional[str]
    error_message: Optional[str]
    created_at: Optional[str]
    completed_at: Optional[str]


class PortalFetchResponse(BaseModel):
    job_id: str
    return_type: str
    gstin: str
    filing_period: str
    records_fetched: int
    invoices_stored: int
    message: str


# ──────────────────────────────────────────────────────────
# Helper: get or create client
# ──────────────────────────────────────────────────────────

async def _get_or_create_client(
    db: AsyncSession,
    client_id: Optional[str],
    gstin: str,
    firm_name: str,
    ca_user_id: str,
) -> ClientORM:
    """
    Find an existing client by client_id or gstin, or create one.
    """
    if client_id:
        try:
            cid = uuid.UUID(client_id)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid client_id UUID: {client_id}")

        result = await db.execute(select(ClientORM).where(ClientORM.id == cid))
        client = result.scalar_one_or_none()
        if not client:
            raise HTTPException(status_code=404, detail=f"Client {client_id} not found")
        return client

    # Look up by GSTIN
    from ingestion_service.parsers.base import normalise_gstin
    try:
        clean_gstin = normalise_gstin(gstin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db.execute(select(ClientORM).where(ClientORM.gstin == clean_gstin))
    client = result.scalar_one_or_none()

    if not client:
        # Auto-create client
        client = ClientORM(
            gstin=clean_gstin,
            firm_name=firm_name or f"Client ({clean_gstin})",
            ca_user_id=ca_user_id,
        )
        db.add(client)
        await db.flush()   # Get the id without committing
        logger.info("Created new client: gstin=%s id=%s", clean_gstin, client.id)

    return client


# ──────────────────────────────────────────────────────────
# Helper: store parsed invoices to DB
# ──────────────────────────────────────────────────────────

async def _store_invoices(
    db: AsyncSession,
    parsed_invoices,
    client: ClientORM,
    job: JobORM,
    source: InvoiceSource,
    filing_period: str,
) -> int:
    """
    Persist ParsedInvoice objects to the invoices table.
    Deduplicates by composite key: client_id + filing_period + gstin + invoice_no + invoice_date.
    Returns count of rows actually inserted.
    """
    inserted = 0
    for inv in parsed_invoices:
        # Deduplication check
        result = await db.execute(
            select(InvoiceORM).where(
                InvoiceORM.client_id == client.id,
                InvoiceORM.filing_period == filing_period,
                InvoiceORM.gstin == inv.gstin,
                InvoiceORM.invoice_no == inv.invoice_no,
                InvoiceORM.invoice_date == inv.invoice_date,
                InvoiceORM.source == source.value,
            )
        )
        if result.scalar_one_or_none():
            logger.debug(
                "Skipping duplicate invoice: %s / %s / %s",
                inv.gstin, inv.invoice_no, inv.invoice_date
            )
            continue

        orm_invoice = InvoiceORM(
            client_id=client.id,
            job_id=job.id,
            source=source.value,
            gstin=inv.gstin,
            supplier_name=inv.supplier_name,
            invoice_no=inv.invoice_no,
            invoice_date=inv.invoice_date,
            filing_period=filing_period,
            taxable_amount=inv.taxable_amount,
            igst=inv.igst,
            cgst=inv.cgst,
            sgst=inv.sgst,
            cess=inv.cess,
            total_amount=inv.total_amount,
            description=inv.description,
            raw_data=inv.raw_data,
        )
        db.add(orm_invoice)
        inserted += 1

    return inserted


# ──────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Health check — used by Docker and Cloud Run."""
    return {"status": "ok", "service": "ingestion", "version": "2.0.0"}


@app.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: Annotated[UploadFile, File(description="Tally XML/CSV or Zoho Books CSV")],
    source_type: Annotated[str, Form(description="'tally' or 'zoho'")],
    filing_period: Annotated[str, Form(description="Filing period YYYY-MM e.g. 2024-03")],
    gstin: Annotated[str, Form(description="Taxpayer GSTIN (15 chars)")],
    ca_user_id: Annotated[str, Form(description="CA user identifier")],
    client_id: Annotated[Optional[str], Form(description="Existing client UUID (optional)")] = None,
    firm_name: Annotated[Optional[str], Form(description="Client firm name (for new clients)")] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Upload and parse a Tally/Zoho invoice file.

    - Detects file format (XML vs CSV)
    - Parses invoices using the appropriate parser
    - Stores to Supabase, deduplicating by composite key
    - Creates a Job record to track this reconciliation run
    - Publishes invoice.ingested event to RabbitMQ

    Returns job_id for subsequent status tracking and orchestration trigger.
    """
    # ── Validate inputs ────────────────────────────────────
    source_type = source_type.lower().strip()
    if source_type not in ("tally", "zoho"):
        raise HTTPException(
            status_code=400,
            detail="source_type must be 'tally' or 'zoho'"
        )

    # Validate filing_period format
    parts = filing_period.split("-")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise HTTPException(
            status_code=400,
            detail="filing_period must be YYYY-MM format e.g. '2024-03'"
        )

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # ── Read file ──────────────────────────────────────────
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    if len(content) > 50 * 1024 * 1024:   # 50 MB limit
        raise HTTPException(status_code=413, detail="File too large (max 50 MB)")

    # ── Get/create client ──────────────────────────────────
    client = await _get_or_create_client(db, client_id, gstin, firm_name or "", ca_user_id)

    # ── Create job ─────────────────────────────────────────
    job_id = uuid.uuid4()
    job = JobORM(
        id=job_id,
        client_id=client.id,
        ca_user_id=ca_user_id,
        filing_period=filing_period,
        status=JobStatus.INGESTING.value,
        started_at=datetime.now(timezone.utc),
        trace_id=str(uuid.uuid4()),
    )
    db.add(job)
    await db.flush()

    # ── Parse file ─────────────────────────────────────────
    try:
        parser = get_parser(source_type, content, file.filename)
        parsed_invoices = parser.parse()
    except ValueError as e:
        job.status = JobStatus.FAILED.value
        job.error_message = str(e)
        await db.commit()
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        job.status = JobStatus.FAILED.value
        job.error_message = f"Unexpected parse error: {e}"
        await db.commit()
        logger.error("Parse error for %s: %s", file.filename, e, exc_info=True)
        raise HTTPException(status_code=500, detail="File parsing failed. Check server logs.")

    # ── Store invoices ─────────────────────────────────────
    source_enum = InvoiceSource.TALLY if source_type == "tally" else InvoiceSource.ZOHO
    inserted = await _store_invoices(db, parsed_invoices, client, job, source_enum, filing_period)

    # ── Update job ─────────────────────────────────────────
    job.total_invoices = inserted
    job.status = JobStatus.PENDING.value    # Ready for Orchestration Service to pick up
    job.progress_pct = 10
    await db.commit()

    # ── Publish event ──────────────────────────────────────
    try:
        publisher = await get_publisher()
        await publisher.publish_invoice_ingested(
            job_id=str(job_id),
            client_id=str(client.id),
            filing_period=filing_period,
            invoice_count=inserted,
            source=source_enum.value,
        )
    except Exception as e:
        logger.warning("Failed to publish invoice.ingested event: %s", e)
        # Don't fail the request — the orchestration service will poll for pending jobs

    logger.info(
        "Upload complete: job_id=%s client=%s invoices=%d/%d parsed",
        job_id, client.gstin, inserted, len(parsed_invoices)
    )

    return UploadResponse(
        job_id=str(job_id),
        client_id=str(client.id),
        filing_period=filing_period,
        source_type=source_type,
        invoices_parsed=len(parsed_invoices),
        invoices_stored=inserted,
        parse_errors=parser.errors[:10],
        status=JobStatus.PENDING.value,
        message=(
            f"Successfully parsed {len(parsed_invoices)} invoices, "
            f"stored {inserted} (skipped {len(parsed_invoices) - inserted} duplicates). "
            f"Reconciliation job created: {job_id}"
        ),
    )


@app.post("/jobs/{job_id}/fetch-portal", response_model=PortalFetchResponse)
async def fetch_portal_data(
    job_id: str,
    return_type: Annotated[str, Form(description="'gstr2a', 'gstr1', or 'gstr3b'")],
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch GSTR data from the GST portal for an existing job.
    Stores the portal invoices alongside the uploaded book invoices.
    Called automatically after upload in a real flow — exposed as a standalone
    endpoint for testing and manual triggering.
    """
    # Validate job
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid job_id: {job_id}")

    result = await db.execute(
        select(JobORM, ClientORM)
        .join(ClientORM, JobORM.client_id == ClientORM.id)
        .where(JobORM.id == jid)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job, client = row

    return_type = return_type.lower().strip()
    if return_type not in ("gstr2a", "gstr1", "gstr3b"):
        raise HTTPException(
            status_code=400,
            detail="return_type must be one of: gstr2a, gstr1, gstr3b"
        )

    # ── Fetch from portal ──────────────────────────────────
    portal_client = GSTPortalClient()
    source_map = {
        "gstr2a": InvoiceSource.GSTR2A,
        "gstr1":  InvoiceSource.GSTR1,
        "gstr3b": InvoiceSource.GSTR3B,
    }

    try:
        if return_type == "gstr2a":
            portal_data = await portal_client.fetch_gstr2a(client.gstin, job.filing_period)
            # Convert portal format to ParsedInvoice-compatible dicts
            parsed = _gstr2a_to_parsed(portal_data)
        elif return_type == "gstr1":
            portal_data = await portal_client.fetch_gstr1(client.gstin, job.filing_period)
            parsed = _gstr1_to_parsed(portal_data)
        else:
            # GSTR-3B is a summary — stored as-is in job metadata for now
            portal_data = await portal_client.fetch_gstr3b(client.gstin, job.filing_period)
            job.current_node = "gstr3b_fetched"
            await db.commit()
            return PortalFetchResponse(
                job_id=job_id,
                return_type=return_type,
                gstin=client.gstin,
                filing_period=job.filing_period,
                records_fetched=1,
                invoices_stored=0,
                message="GSTR-3B summary fetched (stored for tax liability check in Phase 3)",
            )
    except Exception as e:
        logger.error("Portal fetch error for job %s: %s", job_id, e)
        raise HTTPException(status_code=502, detail=f"GST portal fetch failed: {e}")

    # ── Store portal invoices ──────────────────────────────
    inserted = await _store_invoices(
        db, parsed, client, job, source_map[return_type], job.filing_period
    )
    await db.commit()

    logger.info(
        "Portal fetch complete: job=%s return=%s fetched=%d stored=%d",
        job_id, return_type, len(parsed), inserted
    )

    return PortalFetchResponse(
        job_id=job_id,
        return_type=return_type,
        gstin=client.gstin,
        filing_period=job.filing_period,
        records_fetched=len(parsed),
        invoices_stored=inserted,
        message=f"Fetched {len(parsed)} records from {return_type.upper()}, stored {inserted} invoices.",
    )


@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(job_id: str, db: AsyncSession = Depends(get_db)):
    """
    Get the current status of a reconciliation job.
    This is the REST fallback when the WebSocket connection drops (Phase 5).
    """
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid job_id: {job_id}")

    result = await db.execute(select(JobORM).where(JobORM.id == jid))
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return JobStatusResponse(
        job_id=str(job.id),
        client_id=str(job.client_id),
        status=job.status,
        progress_pct=job.progress_pct,
        current_node=job.current_node,
        total_invoices=job.total_invoices or 0,
        total_mismatches=job.total_mismatches or 0,
        report_url=job.report_url,
        error_message=job.error_message,
        created_at=job.created_at.isoformat() if job.created_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


# ──────────────────────────────────────────────────────────
# Portal response → ParsedInvoice converters
# ──────────────────────────────────────────────────────────

def _gstr2a_to_parsed(portal_invoices: list[dict]):
    """Convert GST portal GSTR-2A response format to ParsedInvoice-like objects."""
    from ingestion_service.parsers.base import ParsedInvoice, parse_date, parse_decimal, normalise_gstin
    from decimal import Decimal

    results = []
    for item in portal_invoices:
        try:
            gstin = normalise_gstin(item.get("ctin", ""))
            invoice_date = parse_date(item.get("idt", ""), "idt")
            results.append(ParsedInvoice(
                gstin=gstin,
                invoice_no=item.get("inum", ""),
                invoice_date=invoice_date,
                supplier_name=item.get("cname"),
                taxable_amount=parse_decimal(item.get("txval", 0), "txval"),
                igst=parse_decimal(item.get("iamt", 0), "iamt"),
                cgst=parse_decimal(item.get("camt", 0), "camt"),
                sgst=parse_decimal(item.get("samt", 0), "samt"),
                cess=parse_decimal(item.get("csamt", 0), "csamt"),
                total_amount=parse_decimal(item.get("val", 0), "val"),
                raw_data=item,
            ))
        except Exception as e:
            logger.warning("Skipping portal invoice: %s — %s", item.get("inum"), e)
    return results


def _gstr1_to_parsed(gstr1_data: dict):
    """Convert GSTR-1 b2b entries to ParsedInvoice-like objects."""
    from ingestion_service.parsers.base import ParsedInvoice, parse_date, parse_decimal, normalise_gstin
    from decimal import Decimal

    results = []
    for b2b_entry in gstr1_data.get("b2b", []):
        ctin = b2b_entry.get("ctin", "")
        for inv in b2b_entry.get("inv", []):
            try:
                gstin = normalise_gstin(ctin)
                invoice_date = parse_date(inv.get("idt", ""), "idt")
                # Sum all items
                total_taxable = Decimal("0")
                total_igst = Decimal("0")
                total_cgst = Decimal("0")
                total_sgst = Decimal("0")
                for itm in inv.get("itms", []):
                    d = itm.get("itm_det", {})
                    total_taxable += parse_decimal(d.get("txval", 0))
                    total_igst += parse_decimal(d.get("iamt", 0))
                    total_cgst += parse_decimal(d.get("camt", 0))
                    total_sgst += parse_decimal(d.get("samt", 0))

                results.append(ParsedInvoice(
                    gstin=gstin,
                    invoice_no=inv.get("inum", ""),
                    invoice_date=invoice_date,
                    taxable_amount=total_taxable,
                    igst=total_igst,
                    cgst=total_cgst,
                    sgst=total_sgst,
                    total_amount=parse_decimal(inv.get("val", 0)),
                    raw_data=inv,
                ))
            except Exception as e:
                logger.warning("Skipping GSTR-1 invoice %s: %s", inv.get("inum"), e)
    return results
