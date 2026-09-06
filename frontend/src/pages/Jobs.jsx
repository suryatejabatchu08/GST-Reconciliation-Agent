// frontend/src/pages/Jobs.jsx
// Full job history list with search and status filter.

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search, Filter, RefreshCw, FileText, Plus } from "lucide-react";
import { listJobs } from "../lib/api";

const STATUS_OPTS = ["all", "pending", "processing", "done", "failed"];
const STATUS_BADGE = {
  pending:    { cls: "badge-default", label: "Pending" },
  processing: { cls: "badge-info",    label: "Processing" },
  done:       { cls: "badge-success", label: "Done" },
  failed:     { cls: "badge-danger",  label: "Failed" },
};
const INR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const fmt = (d) => d ? new Date(d).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "—";

export default function Jobs() {
  const navigate = useNavigate();
  const [jobs, setJobs]       = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch]   = useState("");
  const [status, setStatus]   = useState("all");
  const [error, setError]     = useState("");

  const load = async () => {
    setLoading(true); setError("");
    try {
      const res = await listJobs();
      setJobs(res.data?.jobs ?? []);
    } catch {
      setError("Could not load jobs. Check that the ingestion service is running.");
    } finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  const filtered = jobs.filter(j => {
    const matchStatus = status === "all" || j.status === status;
    const q = search.toLowerCase();
    const matchSearch = !q
      || j.id?.toLowerCase().includes(q)
      || j.gstin?.toLowerCase().includes(q)
      || j.filing_period?.toLowerCase().includes(q);
    return matchStatus && matchSearch;
  });

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h1>Job History</h1>
          <p>All reconciliation jobs — click a row to view details</p>
        </div>
        <div style={{ display: "flex", gap: "0.75rem" }}>
          <button className="btn btn-secondary btn-sm" onClick={load}><RefreshCw size={14} /> Refresh</button>
          <button className="btn btn-primary" onClick={() => navigate("/upload")}><Plus size={16} /> New</button>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {/* Filters */}
      <div className="card" style={{ marginBottom: "1.25rem" }}>
        <div style={{ display: "flex", gap: "1rem", alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ position: "relative", flex: 1, minWidth: 200 }}>
            <Search size={14} style={{ position: "absolute", left: "0.85rem", top: "50%", transform: "translateY(-50%)", color: "var(--text-muted)" }} />
            <input
              className="form-input"
              style={{ paddingLeft: "2.25rem" }}
              placeholder="Search by GSTIN, period, or job ID…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
          </div>
          <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
            <Filter size={14} color="var(--text-muted)" />
            {STATUS_OPTS.map(s => (
              <button
                key={s}
                className={`btn btn-sm ${status === s ? "btn-primary" : "btn-secondary"}`}
                onClick={() => setStatus(s)}
              >
                {s.charAt(0).toUpperCase() + s.slice(1)}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="card">
        <div className="card-header">
          <h3>Jobs {filtered.length !== jobs.length && `(${filtered.length} / ${jobs.length})`}</h3>
        </div>
        {loading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "3rem" }}>
            <div className="spinner" style={{ width: 28, height: 28 }} />
          </div>
        ) : filtered.length === 0 ? (
          <div className="empty-state">
            <FileText size={36} />
            <p style={{ marginTop: "0.5rem" }}>
              {jobs.length === 0 ? "No jobs yet." : "No jobs match your filter."}
            </p>
            {jobs.length === 0 && (
              <button className="btn btn-primary" style={{ marginTop: "1rem" }} onClick={() => navigate("/upload")}>
                <Plus size={16} /> Start a Reconciliation
              </button>
            )}
          </div>
        ) : (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Job ID</th>
                  <th>GSTIN</th>
                  <th>Period</th>
                  <th>Mismatches</th>
                  <th>ITC at Risk</th>
                  <th>Escalations</th>
                  <th>Created</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map(j => {
                  const s = STATUS_BADGE[j.status] ?? STATUS_BADGE.pending;
                  return (
                    <tr key={j.id} style={{ cursor: "pointer" }} onClick={() => navigate(`/jobs/${j.id}`)}>
                      <td style={{ fontFamily: "monospace", fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        {j.id?.slice(0, 8)}…
                      </td>
                      <td style={{ color: "var(--text-primary)", fontWeight: 500, fontFamily: "monospace", fontSize: "0.82rem" }}>
                        {j.gstin ?? "—"}
                      </td>
                      <td>{j.filing_period ?? "—"}</td>
                      <td>{j.mismatch_count ?? 0}</td>
                      <td style={{ color: j.total_itc_risk > 0 ? "var(--warning)" : "inherit" }}>
                        {j.total_itc_risk ? INR.format(j.total_itc_risk) : "—"}
                      </td>
                      <td style={{ color: j.escalation_count > 0 ? "var(--danger)" : "inherit" }}>
                        {j.escalation_count ?? 0}
                      </td>
                      <td>{fmt(j.created_at)}</td>
                      <td><span className={`badge ${s.cls}`}>{s.label}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
