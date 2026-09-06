// frontend/src/lib/websocket.js
// Custom React hook for the Gateway WebSocket connection.
// Automatically reconnects with exponential backoff on disconnect.

import { useEffect, useRef, useCallback, useState } from "react";
import { supabase } from "./supabase";

const WS_BASE = import.meta.env.VITE_WS_URL || "ws://localhost:8080";

export function useJobWebSocket(jobId, { onProgress, onReportReady, onConnected } = {}) {
  const wsRef = useRef(null);
  const pingRef = useRef(null);
  const retryRef = useRef(0);
  const [connected, setConnected] = useState(false);

  const connect = useCallback(async () => {
    if (!jobId) return;

    // Get current JWT for WebSocket auth query param
    const { data: { session } } = await supabase.auth.getSession();
    const token = session?.access_token;
    const url = token
      ? `${WS_BASE}/ws/jobs/${jobId}?token=${token}`
      : `${WS_BASE}/ws/jobs/${jobId}`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      retryRef.current = 0;
      // Ping every 30s to keep connection alive through proxies
      pingRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send("ping");
      }, 30000);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "connected") onConnected?.(msg);
        else if (msg.type === "progress") onProgress?.(msg);
        else if (msg.type === "report_ready") onReportReady?.(msg);
        // ignore pong and unknown types
      } catch (_) {}
    };

    ws.onclose = () => {
      setConnected(false);
      clearInterval(pingRef.current);
      // Exponential backoff: 1s, 2s, 4s, 8s ... max 30s
      const delay = Math.min(1000 * 2 ** retryRef.current, 30000);
      retryRef.current += 1;
      setTimeout(connect, delay);
    };

    ws.onerror = () => ws.close();
  }, [jobId, onProgress, onReportReady, onConnected]);

  useEffect(() => {
    connect();
    return () => {
      clearInterval(pingRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { connected };
}
