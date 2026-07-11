"""
report_service/main.py
Report Service — FastAPI app + RabbitMQ consumer.
Runs on port 8004.

Endpoints:
  GET  /health
  GET  /jobs/{job_id}/report/pdf      ← download PDF
  GET  /jobs/{job_id}/report/excel    ← download Excel
  POST /jobs/{job_id}/report/generate ← manually trigger report generation
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from shared.config import get_settings
from shared.db import create_tables, dispose_engine
from shared.publisher import get_publisher, close_publisher
from shared.tracing import setup_tracing
from report_service.consumer import ReportConsumer
from report_service.pdf_builder import build_pdf_report
from report_service.excel_builder import build_excel_report

logging.basicConfig(
    level=getattr(logging, get_settings().log_level, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("report_service")
settings = get_settings()
REPORTS_DIR = Path("reports")

_consumer: Optional[ReportConsumer] = None
_consumer_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer, _consumer_task
    setup_tracing("report-service")
    logger.info("Report Service starting up...")
    REPORTS_DIR.mkdir(exist_ok=True)

    if settings.is_development:
        await create_tables()

    try:
        await get_publisher()
        _consumer = ReportConsumer()
        _consumer_task = asyncio.create_task(_consumer.start())
        logger.info("Report consumer started")
    except Exception as e:
        logger.warning("RabbitMQ not available: %s — HTTP-only mode", e)

    yield

    if _consumer:
        await _consumer.stop()
    if _consumer_task:
        _consumer_task.cancel()
    await close_publisher()
    await dispose_engine()
    logger.info("Report Service shut down.")


app = FastAPI(
    title="GST Reconciliation — Report Service",
    description="Generates PDF and Excel reconciliation reports.",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "report",
        "version": "2.0.0",
        "consumer_running": _consumer_task is not None and not _consumer_task.done(),
    }


@app.get("/jobs/{job_id}/report/pdf")
async def download_pdf(job_id: str):
    """Download the PDF report for a completed job."""
    job_dir = REPORTS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail=f"No report found for job {job_id}")

    pdf_files = list(job_dir.glob("*.pdf"))
    if not pdf_files:
        raise HTTPException(status_code=404, detail="PDF report not yet generated")

    # Return the most recent PDF
    pdf_path = sorted(pdf_files)[-1]
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=pdf_path.name,
    )


@app.get("/jobs/{job_id}/report/excel")
async def download_excel(job_id: str):
    """Download the Excel report for a completed job."""
    job_dir = REPORTS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail=f"No report found for job {job_id}")

    xlsx_files = list(job_dir.glob("*.xlsx"))
    if not xlsx_files:
        raise HTTPException(status_code=404, detail="Excel report not yet generated")

    xlsx_path = sorted(xlsx_files)[-1]
    return FileResponse(
        path=str(xlsx_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=xlsx_path.name,
    )


class GenerateRequest(BaseModel):
    job_id: str
    client_id: str
    filing_period: str
    total_invoices: int = 0


@app.post("/jobs/{job_id}/report/generate")
async def generate_report(job_id: str, req: GenerateRequest):
    """
    Manually trigger report generation for a job.
    Useful for testing or regenerating a report without RabbitMQ.
    """
    payload = {
        "job_id": req.job_id,
        "client_id": req.client_id,
        "filing_period": req.filing_period,
        "total_invoices": req.total_invoices,
        "status": "reconciliation_done",
    }
    if _consumer:
        asyncio.create_task(_consumer._generate_reports(payload))
        return {"message": f"Report generation started for job {job_id}", "status": "generating"}
    else:
        raise HTTPException(status_code=503, detail="Consumer not running — RabbitMQ not connected")
