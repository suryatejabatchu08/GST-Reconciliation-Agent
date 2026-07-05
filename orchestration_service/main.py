"""
orchestration_service/main.py
Orchestration Service — FastAPI application + RabbitMQ consumer startup.

Responsibilities:
  - Start the RabbitMQ consumer on startup (in a background task)
  - Expose /health and /jobs/{job_id}/trigger endpoints
  - Expose /usage/llm endpoint for monitoring LLM rate limit usage

Runs on port: 8002
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from shared.config import get_settings
from shared.db import create_tables, dispose_engine
from shared.publisher import get_publisher, close_publisher
from shared.tracing import setup_tracing
from orchestration_service.consumer import OrchestrationConsumer
from orchestration_service.graph import get_graph, run_reconciliation
from orchestration_service.llm_gateway import get_gateway

logging.basicConfig(
    level=getattr(logging, get_settings().log_level, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("orchestration_service")
settings = get_settings()

_consumer: Optional[OrchestrationConsumer] = None
_consumer_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: compile graph + connect RabbitMQ consumer."""
    global _consumer, _consumer_task
    setup_tracing("orchestration-service")
    logger.info("Orchestration Service starting up...")

    if settings.is_development:
        await create_tables()

    # Pre-compile the graph (validates all imports at startup)
    try:
        get_graph()
        logger.info("LangGraph compiled successfully")
    except Exception as e:
        logger.error("Failed to compile LangGraph: %s", e)

    # Start RabbitMQ consumer in background
    try:
        await get_publisher()
        _consumer = OrchestrationConsumer()
        _consumer_task = asyncio.create_task(_consumer.start())
        logger.info("RabbitMQ consumer started")
    except Exception as e:
        logger.warning("RabbitMQ not available: %s — manual trigger endpoint available", e)

    yield

    # Shutdown
    if _consumer:
        await _consumer.stop()
    if _consumer_task:
        _consumer_task.cancel()
    await close_publisher()
    await dispose_engine()
    logger.info("Orchestration Service shut down.")


app = FastAPI(
    title="GST Reconciliation — Orchestration Service",
    description="LangGraph AI pipeline: normalise → match → classify mismatches.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TriggerRequest(BaseModel):
    job_id: str
    client_id: str
    gstin: str
    filing_period: str
    ca_user_id: str = ""


class TriggerResponse(BaseModel):
    job_id: str
    message: str
    status: str


@app.get("/health")
async def health():
    """Health check."""
    return {
        "status": "ok",
        "service": "orchestration",
        "version": "2.0.0",
        "consumer_running": _consumer_task is not None and not _consumer_task.done(),
    }


@app.post("/jobs/trigger", response_model=TriggerResponse)
async def trigger_job(req: TriggerRequest):
    """
    Manually trigger reconciliation for a job.
    Normally triggered automatically via RabbitMQ event.
    Useful for testing without RabbitMQ running.
    """
    logger.info("Manual trigger: job=%s", req.job_id)

    # Run in background — don't block the HTTP response
    asyncio.create_task(
        run_reconciliation(
            job_id=req.job_id,
            client_id=req.client_id,
            gstin=req.gstin,
            filing_period=req.filing_period,
            ca_user_id=req.ca_user_id,
        )
    )

    return TriggerResponse(
        job_id=req.job_id,
        message=f"Reconciliation started for job {req.job_id}",
        status="started",
    )


@app.get("/usage/llm")
async def llm_usage():
    """Current LLM usage stats — calls today, circuit breaker state."""
    gateway = get_gateway()
    return gateway.usage_stats
