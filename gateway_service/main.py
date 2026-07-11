"""
gateway_service/main.py
API Gateway Service — single entry point for all frontend requests.
Runs on port 8080.

Routes:
  WebSocket:
    WS  /ws/jobs/{job_id}        ← real-time job progress stream

  Proxy routes (all require auth):
    POST /api/ingestion/upload    → Ingestion Service (8001)
    GET  /api/ingestion/jobs      → Ingestion Service (8001)
    ANY  /api/orchestration/*     → Orchestration Service (8002)
    ANY  /api/notifications/*     → Notification Service (8003)
    ANY  /api/reports/*           → Report Service (8004)

  Public:
    GET  /health                  ← Gateway health (checks all services)
    GET  /services                ← Service status overview
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import (
    Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect,
    HTTPException
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from shared.config import get_settings
from shared.tracing import setup_tracing
from shared.publisher import get_publisher, close_publisher
from gateway_service.auth import TokenPayload, require_auth, optional_auth
from gateway_service.consumer import GatewayConsumer
from gateway_service.proxy import proxy_request, close_client, SERVICE_MAP
from gateway_service.websocket_manager import get_ws_manager

logging.basicConfig(
    level=getattr(logging, get_settings().log_level, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gateway_service")
settings = get_settings()

_consumer: Optional[GatewayConsumer] = None
_consumer_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer, _consumer_task
    setup_tracing("gateway-service")
    logger.info("API Gateway starting on port %d", settings.gateway_port)

    try:
        await get_publisher()
        _consumer = GatewayConsumer()
        _consumer_task = asyncio.create_task(_consumer.start())
        logger.info("Gateway RabbitMQ consumer started")
    except Exception as e:
        logger.warning("RabbitMQ not available: %s — WebSocket push disabled", e)

    yield

    if _consumer:
        await _consumer.stop()
    if _consumer_task:
        _consumer_task.cancel()
    await close_publisher()
    await close_client()
    logger.info("API Gateway shut down.")


app = FastAPI(
    title="GST Reconciliation — API Gateway",
    description="Single entry point: proxies API calls, streams WebSocket progress.",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — allow the React/Next.js frontend (localhost:3000 in dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",   # Vite dev server
        "https://your-frontend.vercel.app",  # Update with actual prod URL
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── WebSocket endpoint ────────────────────────────────────

@app.websocket("/ws/jobs/{job_id}")
async def websocket_job_progress(
    job_id: str,
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),  # ?token=xxx for WS auth
):
    """
    Real-time job progress stream.
    Connect with: ws://localhost:8080/ws/jobs/{job_id}?token=<jwt>

    Messages pushed:
      {"type": "connected", "job_id": "...", ...}
      {"type": "progress", "node": "normalise", "progress_pct": 25, ...}
      {"type": "progress", "node": "classifier", "progress_pct": 80, ...}
      {"type": "report_ready", "pdf_path": "...", "excel_path": "...", ...}

    Client can send "ping" to keep connection alive.
    """
    # Auth via query param (browsers can't set headers on WS upgrade)
    if settings.supabase_jwt_secret and not token:
        await websocket.close(code=4001, reason="Authentication required")
        return

    manager = get_ws_manager()
    await manager.connect(job_id, websocket)
    logger.info("WS client connected to job=%s (total ws=%d)", job_id, manager.total_connections)

    await manager.keep_alive(job_id, websocket)


# ── Health endpoints ──────────────────────────────────────

@app.get("/health")
async def health():
    """Gateway health — checks itself only."""
    return {
        "status": "ok",
        "service": "gateway",
        "version": "2.0.0",
        "websocket_connections": get_ws_manager().total_connections,
        "active_jobs": get_ws_manager().active_jobs,
        "consumer_running": _consumer_task is not None and not _consumer_task.done(),
    }


@app.get("/services")
async def service_status():
    """
    Check health of all downstream services.
    Useful for the frontend to show which services are up.
    """
    results = {}
    async with httpx.AsyncClient(timeout=2.0) as client:
        for name, base_url in SERVICE_MAP.items():
            try:
                resp = await client.get(f"{base_url}/health")
                results[name] = {
                    "status": "up" if resp.status_code == 200 else "degraded",
                    "code": resp.status_code,
                }
            except Exception:
                results[name] = {"status": "down", "code": None}

    all_up = all(s["status"] == "up" for s in results.values())
    return {"gateway": "up", "services": results, "overall": "ok" if all_up else "degraded"}


# ── Proxy routes ──────────────────────────────────────────

# Ingestion Service
@app.api_route("/api/ingestion/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_ingestion(
    request: Request,
    path: str,
    user: TokenPayload = Depends(require_auth),
):
    return await proxy_request(request, "ingestion", f"/{path}")


# Orchestration Service
@app.api_route("/api/orchestration/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_orchestration(
    request: Request,
    path: str,
    user: TokenPayload = Depends(require_auth),
):
    return await proxy_request(request, "orchestration", f"/{path}")


# Notification Service
@app.api_route("/api/notifications/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_notifications(
    request: Request,
    path: str,
    user: TokenPayload = Depends(require_auth),
):
    return await proxy_request(request, "notifications", f"/{path}")


# Report Service
@app.api_route("/api/reports/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_reports(
    request: Request,
    path: str,
    user: TokenPayload = Depends(require_auth),
):
    return await proxy_request(request, "reports", f"/{path}")


# ── 404 handler ───────────────────────────────────────────

@app.exception_handler(404)
async def not_found(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=404,
        content={
            "detail": f"Route '{request.url.path}' not found on the gateway.",
            "hint": "Available prefixes: /api/ingestion/, /api/orchestration/, "
                    "/api/reports/, /api/notifications/, /ws/jobs/{job_id}",
        }
    )
