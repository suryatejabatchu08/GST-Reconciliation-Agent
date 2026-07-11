"""
gateway_service/tests/test_websocket_manager.py
Unit tests for the WebSocket connection manager.
Uses mock WebSocket objects — no real network connections.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway_service.websocket_manager import WebSocketManager


def make_mock_ws():
    """Create a mock WebSocket that records sent messages."""
    ws = AsyncMock()
    ws.sent_messages = []

    async def capture_send(text):
        ws.sent_messages.append(json.loads(text))

    ws.send_text = capture_send
    ws.accept = AsyncMock()
    return ws


class TestWebSocketManager:

    @pytest.mark.asyncio
    async def test_connect_accepts_websocket(self):
        manager = WebSocketManager()
        ws = make_mock_ws()
        await manager.connect("job-001", ws)
        ws.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_sends_acknowledgement(self):
        manager = WebSocketManager()
        ws = make_mock_ws()
        await manager.connect("job-001", ws)
        assert len(ws.sent_messages) == 1
        assert ws.sent_messages[0]["type"] == "connected"
        assert ws.sent_messages[0]["job_id"] == "job-001"

    @pytest.mark.asyncio
    async def test_broadcast_reaches_connected_client(self):
        manager = WebSocketManager()
        ws = make_mock_ws()
        await manager.connect("job-001", ws)

        await manager.broadcast("job-001", {"type": "progress", "progress_pct": 50})

        # First message is the connect ack, second is the broadcast
        assert len(ws.sent_messages) == 2
        assert ws.sent_messages[1]["type"] == "progress"
        assert ws.sent_messages[1]["progress_pct"] == 50

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_clients(self):
        manager = WebSocketManager()
        ws1 = make_mock_ws()
        ws2 = make_mock_ws()
        await manager.connect("job-001", ws1)
        await manager.connect("job-001", ws2)

        await manager.broadcast("job-001", {"type": "progress", "node": "normalise"})

        assert len(ws1.sent_messages) == 2   # connect ack + progress
        assert len(ws2.sent_messages) == 2

    @pytest.mark.asyncio
    async def test_broadcast_only_reaches_correct_job(self):
        manager = WebSocketManager()
        ws_job1 = make_mock_ws()
        ws_job2 = make_mock_ws()
        await manager.connect("job-001", ws_job1)
        await manager.connect("job-002", ws_job2)

        await manager.broadcast("job-001", {"type": "progress"})

        assert len(ws_job1.sent_messages) == 2   # connect + progress
        assert len(ws_job2.sent_messages) == 1   # connect only (not their job)

    @pytest.mark.asyncio
    async def test_broadcast_to_nonexistent_job_does_nothing(self):
        """Broadcasting to a job with no clients should not raise."""
        manager = WebSocketManager()
        # Should complete without error
        await manager.broadcast("nonexistent-job", {"type": "progress"})

    @pytest.mark.asyncio
    async def test_disconnect_removes_connection(self):
        manager = WebSocketManager()
        ws = make_mock_ws()
        await manager.connect("job-001", ws)
        assert manager.total_connections == 1

        await manager.disconnect("job-001", ws)
        assert manager.total_connections == 0

    @pytest.mark.asyncio
    async def test_stale_connections_cleaned_on_broadcast(self):
        """Connections that throw on send should be removed automatically."""
        manager = WebSocketManager()
        ws = make_mock_ws()
        await manager.connect("job-001", ws)

        # Make the WebSocket throw on next send (simulating disconnect)
        async def raise_on_send(text):
            raise RuntimeError("Connection closed")
        ws.send_text = raise_on_send

        # Broadcast should not raise, and should clean up the stale connection
        await manager.broadcast("job-001", {"type": "progress"})
        assert manager.total_connections == 0

    @pytest.mark.asyncio
    async def test_total_connections_counts_all_jobs(self):
        manager = WebSocketManager()
        ws1 = make_mock_ws()
        ws2 = make_mock_ws()
        ws3 = make_mock_ws()

        await manager.connect("job-001", ws1)
        await manager.connect("job-001", ws2)
        await manager.connect("job-002", ws3)

        assert manager.total_connections == 3

    @pytest.mark.asyncio
    async def test_active_jobs_lists_job_ids(self):
        manager = WebSocketManager()
        ws1 = make_mock_ws()
        ws2 = make_mock_ws()

        await manager.connect("job-aaa", ws1)
        await manager.connect("job-bbb", ws2)

        active = manager.active_jobs
        assert "job-aaa" in active
        assert "job-bbb" in active
        assert len(active) == 2
