// frontend/src/lib/api.js
// Axios client pre-configured to talk to the API Gateway.
// Automatically attaches the Supabase JWT on every request.

import axios from "axios";
import { supabase } from "./supabase";

const GATEWAY = import.meta.env.VITE_GATEWAY_URL || "http://localhost:8080";

const api = axios.create({ baseURL: GATEWAY });

// Attach Bearer token on every request
api.interceptors.request.use(async (config) => {
  const { data: { session } } = await supabase.auth.getSession();
  if (session?.access_token) {
    config.headers.Authorization = `Bearer ${session.access_token}`;
  }
  return config;
});

// ── Ingestion ──────────────────────────────────────────────
export const uploadFiles = (formData, onProgress) =>
  api.post("/api/ingestion/upload", formData, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (e) => onProgress?.(Math.round((e.loaded / e.total) * 100)),
  });

export const listJobs = () => api.get("/api/ingestion/jobs");

// ── Orchestration ──────────────────────────────────────────
export const getJob = (jobId) => api.get(`/api/orchestration/jobs/${jobId}`);
export const startReconciliation = (payload) => api.post("/api/orchestration/jobs", payload);

// ── Reports ───────────────────────────────────────────────
export const downloadPdf = (jobId) =>
  api.get(`/api/reports/jobs/${jobId}/report/pdf`, { responseType: "blob" });

export const downloadExcel = (jobId) =>
  api.get(`/api/reports/jobs/${jobId}/report/excel`, { responseType: "blob" });

// ── Services health ────────────────────────────────────────
export const getServicesHealth = () => axios.get(`${GATEWAY}/services`);

export default api;
