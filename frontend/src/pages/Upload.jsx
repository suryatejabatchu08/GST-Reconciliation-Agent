// frontend/src/pages/Upload.jsx
// File upload page: drag-and-drop Tally XML, Zoho CSV, GST JSON.
// On submit → POST /api/ingestion/upload → redirect to job progress page.

import { useState, useRef, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { Upload as UploadIcon, File, X, ArrowRight, Info } from "lucide-react";
import { uploadFiles } from "../lib/api";
import "./Upload.css";

const ACCEPTED = ".xml,.csv,.json,.xlsx";
const FILE_TYPES = [
  { label: "Tally XML",   ext: ".xml",  desc: "Export from Tally Prime / ERP 9" },
  { label: "Zoho CSV",    ext: ".csv",  desc: "Purchase register from Zoho Books" },
  { label: "GST JSON",    ext: ".json", desc: "GSTR-2A / 3B download from GST Portal" },
];

const fmtSize = (b) => b < 1024 * 1024 ? `${(b/1024).toFixed(0)} KB` : `${(b/1024/1024).toFixed(1)} MB`;

export default function Upload() {
  const navigate = useNavigate();
  const inputRef = useRef(null);
  const [files, setFiles] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [uploadPct, setUploadPct] = useState(null);
  const [error, setError] = useState("");
  const [gstin, setGstin] = useState("");
  const [period, setPeriod] = useState("");

  const addFiles = (incoming) => {
    const newFiles = Array.from(incoming).filter(
      f => !files.find(ex => ex.name === f.name)
    );
    setFiles(prev => [...prev, ...newFiles]);
  };

  const removeFile = (name) => setFiles(prev => prev.filter(f => f.name !== name));

  const onDrop = useCallback((e) => {
    e.preventDefault(); setDragging(false);
    addFiles(e.dataTransfer.files);
  }, [files]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!files.length) { setError("Please add at least one file."); return; }
    setError(""); setUploadPct(0);

    const fd = new FormData();
    files.forEach(f => fd.append("files", f));
    if (gstin) fd.append("gstin", gstin);
    if (period) fd.append("filing_period", period);

    try {
      const res = await uploadFiles(fd, pct => setUploadPct(pct));
      const jobId = res.data?.job_id;
      navigate(jobId ? `/jobs/${jobId}` : "/jobs");
    } catch (err) {
      setError(err.response?.data?.detail ?? "Upload failed. Check that the ingestion service is running.");
      setUploadPct(null);
    }
  };

  return (
    <div>
      <div className="page-header">
        <h1>New Reconciliation</h1>
        <p>Upload your Tally, Zoho, or GST Portal files to start AI reconciliation</p>
      </div>

      <div className="grid-2" style={{ alignItems: "start" }}>

        {/* Left: Upload form */}
        <form onSubmit={handleSubmit}>
          {error && <div className="alert alert-error">{error}</div>}

          {/* Metadata */}
          <div className="card" style={{ marginBottom: "1.25rem" }}>
            <h3 style={{ marginBottom: "1rem" }}>Client Details</h3>
            <div className="grid-2">
              <div className="form-group" style={{ margin: 0 }}>
                <label className="form-label">GSTIN (optional)</label>
                <input
                  className="form-input"
                  placeholder="22AAAAA0000A1Z5"
                  value={gstin}
                  onChange={e => setGstin(e.target.value.toUpperCase())}
                  maxLength={15}
                  style={{ fontFamily: "monospace" }}
                />
              </div>
              <div className="form-group" style={{ margin: 0 }}>
                <label className="form-label">Filing Period (optional)</label>
                <input
                  className="form-input"
                  placeholder="Mar 2024"
                  value={period}
                  onChange={e => setPeriod(e.target.value)}
                />
              </div>
            </div>
          </div>

          {/* Drop zone */}
          <div className="card" style={{ marginBottom: "1.25rem" }}>
            <h3 style={{ marginBottom: "1rem" }}>Upload Files</h3>

            <div
              className={`dropzone ${dragging ? "drag-over" : ""}`}
              onDragEnter={e => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDragOver={e => e.preventDefault()}
              onDrop={onDrop}
              onClick={() => inputRef.current?.click()}
            >
              <input
                ref={inputRef}
                type="file"
                multiple
                accept={ACCEPTED}
                style={{ display: "none" }}
                onChange={e => addFiles(e.target.files)}
              />
              <div className="dropzone-icon">
                <UploadIcon size={24} color="var(--accent-light)" />
              </div>
              <p style={{ color: "var(--text-primary)", fontWeight: 600, marginBottom: "0.25rem" }}>
                Drop files here or click to browse
              </p>
              <p style={{ fontSize: "0.8rem" }}>Supports .xml, .csv, .json, .xlsx</p>
            </div>

            {/* File list */}
            {files.length > 0 && (
              <div className="file-list">
                {files.map(f => (
                  <div className="file-chip" key={f.name}>
                    <File size={14} color="var(--accent-light)" style={{ flexShrink: 0 }} />
                    <span className="file-chip-name">{f.name}</span>
                    <span className="file-chip-size">{fmtSize(f.size)}</span>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      style={{ padding: "0.2rem" }}
                      onClick={() => removeFile(f.name)}
                    >
                      <X size={14} />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* Upload progress */}
            {uploadPct !== null && (
              <div className="upload-progress">
                <div className="upload-pct">
                  <span>Uploading…</span>
                  <span>{uploadPct}%</span>
                </div>
                <div className="progress-track">
                  <div className="progress-fill" style={{ width: `${uploadPct}%` }} />
                </div>
              </div>
            )}
          </div>

          <button
            className="btn btn-primary btn-lg"
            style={{ width: "100%" }}
            disabled={!files.length || uploadPct !== null}
            type="submit"
          >
            {uploadPct !== null ? <span className="spinner" /> : <ArrowRight size={18} />}
            {uploadPct !== null ? "Uploading…" : "Start Reconciliation"}
          </button>
        </form>

        {/* Right: Info panel */}
        <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>

          <div className="card">
            <div style={{ display: "flex", gap: "0.6rem", alignItems: "center", marginBottom: "1rem" }}>
              <Info size={16} color="var(--info)" />
              <h3>Supported File Types</h3>
            </div>
            {FILE_TYPES.map(ft => (
              <div key={ft.ext} style={{ display: "flex", gap: "1rem", padding: "0.65rem 0", borderBottom: "1px solid var(--border)" }}>
                <span className="badge badge-info" style={{ flexShrink: 0 }}>{ft.ext}</span>
                <div>
                  <div style={{ fontSize: "0.85rem", fontWeight: 600, color: "var(--text-primary)" }}>{ft.label}</div>
                  <div style={{ fontSize: "0.78rem", color: "var(--text-muted)" }}>{ft.desc}</div>
                </div>
              </div>
            ))}
            <div style={{ padding: "0.65rem 0", display: "flex", gap: "1rem" }}>
              <span className="badge badge-info" style={{ flexShrink: 0 }}>.xlsx</span>
              <div>
                <div style={{ fontSize: "0.85rem", fontWeight: 600, color: "var(--text-primary)" }}>Excel Workbook</div>
                <div style={{ fontSize: "0.78rem", color: "var(--text-muted)" }}>Generic invoice register format</div>
              </div>
            </div>
          </div>

          <div className="card">
            <h3 style={{ marginBottom: "1rem" }}>What happens next?</h3>
            {[
              ["1", "Files parsed", "Tally XML, Zoho CSV and GST JSON extracted and normalised"],
              ["2", "AI matching", "Gemini normalises supplier names; invoices matched across sources"],
              ["3", "Classification", "Groq classifies each mismatch by cause and severity"],
              ["4", "Reports", "PDF summary + Excel workbook generated for download"],
              ["5", "Notifications", "Escalations emailed to suppliers and CA"],
            ].map(([n, title, desc]) => (
              <div key={n} style={{ display: "flex", gap: "0.85rem", marginBottom: "0.85rem" }}>
                <div style={{
                  width: 24, height: 24, borderRadius: "50%",
                  background: "var(--accent-glow)", border: "1px solid rgba(59,130,246,0.3)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: "0.7rem", fontWeight: 700, color: "var(--accent-light)", flexShrink: 0,
                }}>{n}</div>
                <div>
                  <div style={{ fontSize: "0.85rem", fontWeight: 600, color: "var(--text-primary)" }}>{title}</div>
                  <div style={{ fontSize: "0.78rem", color: "var(--text-muted)" }}>{desc}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
