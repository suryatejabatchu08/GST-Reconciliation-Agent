"""
gateway_service/websocket_manager.py
Manages active WebSocket connections and routes messages to the right clients.

Design:
  - One WebSocket connection per job per browser tab
  - Multiple clients can watch the same job (e.g. CA + client both open the dashboard)
  - Uses asyncio locks for thread-safe updates to the connection registry
  - Messages are JSON — same payload as the RabbitMQ job.progress events

Connection lifecycle:
  1. Client connects: GET /ws/jobs/{job_id}?token=xxx
  2. Auth validated from ?token query param
  3. Connection added to registry: job_id → set of WebSocket connections
  4. Progress events received from RabbitMQ are broadcast to all connections for that job
  5. On disconnect / error, connection is removed from registry

Message format (sent to client):
  {
    "job_id": "uuid",
    "node": "classifier",
    "status": "classifying",
    "progress_pct": 75,
    "message": "Classifying 8 mismatches...",
    "timestamp": "2024-03-15T14:32:00Z"
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class WebSocketManager:
    """
    Thread-safe registry of active WebSocket connections keyed by job_id.

    One manager instance is shared across the entire gateway application
    (module-level singleton pattern).
    """

    def __init__(self):
        # job_id → set of active WebSocket connections
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, job_id: str, websocket: WebSocket) -> None:
        """Accept a WebSocket and register it under the given job_id."""
        await websocket.accept()
        async with self._lock:
            self._connections[job_id].add(websocket)
        logger.info(
            "WebSocket connected: job=%s total_clients=%d",
            job_id, len(self._connections[job_id])
        )
        # Send immediate acknowledgement so client knows it's connected
        await self._send_to(websocket, {
            "type": "connected",
            "job_id": job_id,
            "message": "Connected to live progress stream",
            "timestamp": _now(),
        })

    async def disconnect(self, job_id: str, websocket: WebSocket) -> None:
        """Remove a WebSocket connection from the registry."""
        async with self._lock:
            self._connections[job_id].discard(websocket)
            if not self._connections[job_id]:
                del self._connections[job_id]
        logger.info("WebSocket disconnected: job=%s", job_id)

    async def broadcast(self, job_id: str, message: dict[str, Any]) -> None:
        """
        Send a message to all clients watching a specific job.
        Silently removes stale connections that have already closed.
        """
        async with self._lock:
            clients = set(self._connections.get(job_id, set()))

        if not clients:
            return  # No one watching this job

        stale = set()
        for websocket in clients:
            try:
                await self._send_to(websocket, message)
            except Exception as e:
                logger.debug("WebSocket send failed (stale connection): %s", e)
                stale.add(websocket)

        # Clean up stale connections
        if stale:
            async with self._lock:
                self._connections[job_id] -= stale
                if not self._connections[job_id]:
                    self._connections.pop(job_id, None)

    async def broadcast_all(self, message: dict[str, Any]) -> None:
        """Send a message to ALL connected clients (e.g. system maintenance notice)."""
        async with self._lock:
            all_ws = {ws for clients in self._connections.values() for ws in clients}
        for websocket in all_ws:
            try:
                await self._send_to(websocket, message)
            except Exception:
                pass

    @staticmethod
    async def _send_to(websocket: WebSocket, message: dict) -> None:
        """Send a JSON message to a single WebSocket."""
        await websocket.send_text(json.dumps(message, default=str))

    @property
    def active_jobs(self) -> list[str]:
        """List of job IDs currently being watched."""
        return list(self._connections.keys())

    @property
    def total_connections(self) -> int:
        """Total number of active WebSocket connections across all jobs."""
        return sum(len(clients) for clients in self._connections.values())

    async def keep_alive(self, job_id: str, websocket: WebSocket) -> None:
        """
        Keep the WebSocket open until the client disconnects or sends 'close'.
        Call this after connect() in the WebSocket route handler.
        """
        try:
            while True:
                # Wait for client message (ping or close)
                data = await websocket.receive_text()
                if data == "ping":
                    await self._send_to(websocket, {"type": "pong", "timestamp": _now()})
        except WebSocketDisconnect:
            await self.disconnect(job_id, websocket)
        except Exception as e:
            logger.warning("WebSocket error for job=%s: %s", job_id, e)
            await self.disconnect(job_id, websocket)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Module-level singleton ─────────────────────────────────
_manager: WebSocketManager | None = None


def get_ws_manager() -> WebSocketManager:
    """Return the module-level WebSocketManager singleton."""
    global _manager
    if _manager is None:
        _manager = WebSocketManager()
    return _manager
