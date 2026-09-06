// frontend/src/pages/Reconciliation.jsx
// Job detail page: live WebSocket progress + mismatch table + report download.

import { useEffect, useState, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  ArrowLeft, Download, FileText, FileSpreadsheet,
  Wifi, WifiOff, CheckCircle, AlertTriangle, Clock, Loader
} from "lucide-react";
import MismatchTable from "../components/MismatchTable";
import { useJobWebSocket } from "../lib/websocket";
import { getJob, downloadPdf, downloadExcel } from "../lib/api";

const INR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const fmt = (d) => d ? new Date(d).toLocaleString("en-IN") : "—";

// Ordered list of pipeline nodes for progress display
const NODES = ["normalise", "gstr2a_matcher", "gstr1_matcher", "bank_matcher",
               "validate_matches", "tax_head_checker", "classifier"];

const NODE_LABELS = {
  normalise:        "Normalising invoices",
  gstr2a_matcher:   "Matching GSTR-2A",
  gstr1_matcher:    "Matching GSTR-1",
  bank_matcher:     "Matching bank records",
  validate_matches: "Validating matches",
  tax_head_checker: "Checking tax heads",
  classifier:       "Classifying mismatches",
};

function blob_download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

export default function Reconciliation() {
  const { jobId } = useParams();
  const navigate = useNavigate();

  const [job, setJob] = useState(null);
  const [progress, setProgress] = useState({ pct: 0, node: "", message: "" });
  const [reportReady, setReportReady] = useState(null);
  const [downloading, setDownloading] = useState("");
  const [loadingJob, setLoadingJob] = useState(true);

  // Load job details
  useEffect(() => {
    if (!jobId) return;
    getJob(jobId)
      .then(r => { setJob(r.data); if (r.data?.status === "done") setProgress({ pct: 100, node: "done", message: "Reconciliation complete" }); })
      .catch(() => {})
      .finally(() => setLoadingJob(false));
  }, [jobId]);

  // WebSocket callbacks
  const onProgress = useCallback((msg) => {
    setProgress({ pct: msg.progress_pct ?? 0, node: msg.node ?? "", message: msg.message ?? "" });
  }, []);

  const onReportReady = useCallback((msg) => {
    setReportReady(msg);
    setProgress({ pct: 100, node: "done", message: "Reconciliation complete" });
    // Reload job to get mismatch list
    getJob(jobId).then(r => setJob(r.data)).catch(() => {});
  }, [jobId]);

  const { connected } = useJobWebSocket(jobId, { onProgress, onReportReady });

  const handleDownload = async (type) => {
    setDownloading(type);
    try {
      const res = type === "pdf" ? await downloadPdf(jobId) : await downloadExcel(jobId);
      const ext = type === "pdf" ? "pdf" : "xlsx";
      blob_download(res.data, `reconciliation_${jobId.slice(0, 8)}.${ext}`);
    } catch (e) {
      alert("Download failed. Report may not be ready yet.");
    } finally {
      setDownloading("");
    }
  };

  if (loadingJob) {
    return (
      <div style={{ display: "flex", justifyContent: "center", paddingTop: "4rem" }}>
        <div className="spinner" style={{ width: 32, height: 32 }} />
      </div>
    );
  }

  const isDone = progress.pct === 100 || job?.status === "done";
  const mismatches = job?.mismatches ?? [];

  return (
    <div>
      {/* Header */}
      <div className="page-header" style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
        <div>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate("/jobs")} style={{ marginBottom: "0.5rem" }}>
            <ArrowLeft size={14} /> Back to Jobs
          </button>
          <h1>Reconciliation Job</h1>
          <p style={{ fontFamily: "monospace", fontSize: "0.82rem" }}>ID: {jobId}</p>
        </div>

        {/* WS status */}
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          {connected
            ? <><Wifi size={14} color="var(--success)" /><span style={{ fontSize: "0.78rem", color: "var(--success)" }}>Live</span></>
            : <><WifiOff size={14} color="var(--text-muted)" /><span style={{ fontSize: "0.78rem", color: "var(--text-muted)" }}>Reconnecting…</span></>
          }
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>

        {/* Progress card */}
        {!isDone && (
          <div className="card">
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1.5rem" }}>
              <div className="spinner" />
              <div>
                <h3>Reconciliation in progress…</h3>
                <p style={{ fontSize: "0.82rem", margin: 0 }}>{progress.message || "Starting up…"}</p>
              </div>
            </div>

            {/* Progress bar */}
            <div style={{ marginBottom: "1.25rem" }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8rem", color: "var(--text-secondary)", marginBottom: "0.4rem" }}>
                <span>{progress.node ? NODE_LABELS[progress.node] ?? progress.node : "Initialising"}</span>
                <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>{progress.pct}%</span>
              </div>
              <div className="progress-track" style={{ height: 8 }}>
                <div className="progress-fill" style={{ width: `${progress.pct}%` }} />
              </div>
            </div>

            {/* Node steps */}
            <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
              {NODES.map(n => {
                const nodeIdx = NODES.indexOf(n);
                const currIdx = NODES.indexOf(progress.node);
                const done = currIdx > nodeIdx;
                const active = currIdx === nodeIdx;
                return (
                  <div key={n} style={{
                    display: "flex", alignItems: "center", gap: "0.35rem",
                    padding: "0.3rem 0.7rem",
                    borderRadius: 99,
                    background: done ? "var(--success-bg)" : active ? "var(--accent-glow)" : "var(--bg-surface)",
                    border: `1px solid ${done ? "rgba(16,185,129,0.3)" : active ? "rgba(59,130,246,0.3)" : "var(--border)"}`,
                    fontSize: "0.72rem",
                    color: done ? "var(--success)" : active ? "var(--accent-light)" : "var(--text-muted)",
                    fontWeight: active || done ? 600 : 400,
                    transition: "all 0.3s ease",
                  }}>
                    {done ? <CheckCircle size={10} /> : active ? <Loader size={10} className="pulse" /> : null}
                    {NODE_LABELS[n] ?? n}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Done banner */}
        {isDone && (
          <div className="alert alert-success" style={{ alignItems: "center" }}>
            <CheckCircle size={18} />
            <div style={{ flex: 1 }}>
              <strong>Reconciliation complete!</strong>
              <span style={{ marginLeft: "0.5rem", opacity: 0.8, fontSize: "0.85rem" }}>
                Found {mismatches.length} mismatches.
              </span>
            </div>
            <div style={{ display: "flex", gap: "0.5rem", flexShrink: 0 }}>
              <button
                className="btn btn-secondary btn-sm"
                disabled={downloading === "pdf"}
                onClick={() => handleDownload("pdf")}
              >
                {downloading === "pdf" ? <span className="spinner" /> : <FileText size={14} />}
                PDF
              </button>
              <button
                className="btn btn-secondary btn-sm"
                disabled={downloading === "excel"}
                onClick={() => handleDownload("excel")}
              >
                {downloading === "excel" ? <span className="spinner" /> : <FileSpreadsheet size={14} />}
                Excel
              </button>
            </div>
          </div>
        )}

        {/* Job metadata */}
        {job && (
          <div className="card">
            <h3 style={{ marginBottom: "1rem" }}>Job Details</h3>
            <div className="grid-4">
              {[
                ["GSTIN",         job.gstin ?? "—"],
                ["Filing Period", job.filing_period ?? "—"],
                ["Created",       fmt(job.created_at)],
                ["Status",        job.status],
                ["Mismatches",    job.mismatch_count ?? mismatches.length],
                ["Escalations",   job.escalation_count ?? "—"],
                ["ITC at Risk",   job.total_itc_risk ? INR.format(job.total_itc_risk) : "—"],
                ["Auto-resolved", job.auto_count ?? "—"],
              ].map(([l, v]) => (
                <div key={l}>
                  <div style={{ fontSize: "0.72rem", fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "0.25rem" }}>{l}</div>
                  <div style={{ fontSize: "0.9rem", color: "var(--text-primary)", fontWeight: 500 }}>{v}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Mismatch table */}
        <div className="card">
          <div className="card-header">
            <h3>Mismatches ({mismatches.length})</h3>
            <div style={{ display: "flex", gap: "0.5rem" }}>
              {isDone && (
                <>
                  <button className="btn btn-secondary btn-sm" disabled={downloading === "pdf"} onClick={() => handleDownload("pdf")}>
                    <FileText size={14} /> Download PDF
                  </button>
                  <button className="btn btn-secondary btn-sm" disabled={downloading === "excel"} onClick={() => handleDownload("excel")}>
                    <FileSpreadsheet size={14} /> Download Excel
                  </button>
                </>
              )}
            </div>
          </div>
          {!isDone && mismatches.length === 0 ? (
            <div className="empty-state">
              <p>Mismatches will appear here as the reconciliation progresses.</p>
            </div>
          ) : (
            <MismatchTable mismatches={mismatches} />
          )}
        </div>
      </div>
    </div>
  );
}
