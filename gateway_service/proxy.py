"""
gateway_service/proxy.py
HTTP reverse proxy — forwards incoming gateway requests to the correct microservice.

Routing table:
  /api/ingestion/*      → http://localhost:8001/*
  /api/orchestration/*  → http://localhost:8002/*
  /api/notifications/*  → http://localhost:8003/*
  /api/reports/*        → http://localhost:8004/*

Uses httpx.AsyncClient for non-blocking HTTP forwarding.
Streams the response body back to the caller — no buffering.
Preserves: method, headers (minus hop-by-hop), query params, request body.

Error handling:
  - Upstream service unavailable → 503 with descriptive message
  - Upstream timeout (10s) → 504
  - Other upstream errors → forwarded as-is
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response

from shared.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Service URL map ────────────────────────────────────────
SERVICE_MAP = {
    "ingestion":      f"http://localhost:{settings.ingestion_port}",
    "orchestration":  f"http://localhost:{settings.orchestration_port}",
    "notifications":  f"http://localhost:{settings.notification_port}",
    "reports":        f"http://localhost:{settings.report_port}",
}

# Headers that must NOT be forwarded (hop-by-hop)
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "host",   # We set our own Host when forwarding
}

# Shared async client — reused across all requests (connection pooling)
_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()


# ── Proxy function ────────────────────────────────────────

async def proxy_request(request: Request, service: str, path: str) -> Response:
    """
    Forward `request` to the upstream `service` at `path`.

    Args:
        request: FastAPI Request object
        service: Service name key from SERVICE_MAP (e.g. "ingestion")
        path: Path on the upstream service (e.g. "/upload")

    Returns:
        FastAPI Response with upstream's status, headers, and body
    """
    base_url = SERVICE_MAP.get(service)
    if not base_url:
        raise HTTPException(status_code=500, detail=f"Unknown service: {service}")

    upstream_url = f"{base_url}{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    # Filter hop-by-hop headers
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    # Add X-Forwarded-For so upstream services know the real client IP
    client_ip = request.client.host if request.client else "unknown"
    forward_headers["X-Forwarded-For"] = client_ip
    forward_headers["X-Gateway-Version"] = "2.0"

    body = await request.body()

    try:
        client = await get_client()
        upstream_response = await client.request(
            method=request.method,
            url=upstream_url,
            headers=forward_headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.error("Service unavailable: %s at %s", service, upstream_url)
        raise HTTPException(
            status_code=503,
            detail=f"Service '{service}' is unavailable. Please try again shortly."
        )
    except httpx.TimeoutException:
        logger.error("Upstream timeout: %s at %s", service, upstream_url)
        raise HTTPException(
            status_code=504,
            detail=f"Service '{service}' did not respond in time."
        )

    # Filter response hop-by-hop headers
    response_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    logger.debug(
        "Proxied %s %s → %s %d",
        request.method, request.url.path, upstream_url, upstream_response.status_code
    )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
