// frontend/src/pages/Dashboard.jsx
// Main landing page after login — shows job stats, recent jobs, service health.

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { FileText, AlertTriangle, CheckCircle, Clock, Plus, RefreshCw } from "lucide-react";
import StatCard from "../components/StatCard";
import { listJobs, getServicesHealth } from "../lib/api";

const STATUS_BADGE = {
  pending:    { cls: "badge-default",  label: "Pending" },
  processing: { cls: "badge-info",     label: "Processing" },
  done:       { cls: "badge-success",  label: "Done" },
  failed:     { cls: "badge-danger",   label: "Failed" },
};

const fmt = (d) => d ? new Date(d).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "—";

export default function Dashboard() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState([]);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = async () => {
    setLoading(true); setError("");
    try {
      const [jobsRes, healthRes] = await Promise.allSettled([
        listJobs(),
        getServicesHealth(),
      ]);
      if (jobsRes.status === "fulfilled") setJobs(jobsRes.value.data?.jobs ?? []);
      if (healthRes.status === "fulfilled") setHealth(healthRes.value.data);
    } catch (e) {
      setError("Could not load dashboard. Make sure all services are running.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const doneJobs = jobs.filter(j => j.status === "done");
  const totalMismatches = doneJobs.reduce((s, j) => s + (j.mismatch_count ?? 0), 0);
  const escalations = doneJobs.reduce((s, j) => s + (j.escalation_count ?? 0), 0);

  return (
    <div>
      {/* Header */}
      <div className="page-header" style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
        <div>
          <h1>Dashboard</h1>
          <p>Overview of your GST reconciliation jobs</p>
        </div>
        <div style={{ display: "flex", gap: "0.75rem" }}>
          <button className="btn btn-secondary btn-sm" onClick={load}>
            <RefreshCw size={14} /> Refresh
          </button>
          <button className="btn btn-primary" onClick={() => navigate("/upload")}>
            <Plus size={16} /> New Reconciliation
          </button>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {/* Stats */}
      <div className="grid-4" style={{ marginBottom: "2rem" }}>
        <StatCard icon={FileText}      label="Total Jobs"    value={jobs.length}      sub="All time"              color="accent" />
        <StatCard icon={CheckCircle}   label="Completed"     value={doneJobs.length}  sub="Successfully reconciled" color="success" />
        <StatCard icon={AlertTriangle} label="Mismatches"    value={totalMismatches}  sub="Across all jobs"        color="warning" />
        <StatCard icon={Clock}         label="Escalations"   value={escalations}      sub="Need CA attention"     color="danger" />
      </div>

      <div className="grid-2">
        {/* Recent Jobs */}
        <div className="card" style={{ gridColumn: "1 / -1" }}>
          <div className="card-header">
            <h3>Recent Jobs</h3>
            <button className="btn btn-ghost btn-sm" onClick={() => navigate("/jobs")}>
              View all →
            </button>
          </div>

          {loading ? (
            <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
              <div className="spinner" />
            </div>
          ) : jobs.length === 0 ? (
            <div className="empty-state">
              <FileText size={40} />
              <p style={{ marginTop: "0.5rem" }}>No jobs yet. Start your first reconciliation.</p>
              <button className="btn btn-primary" style={{ marginTop: "1rem" }} onClick={() => navigate("/upload")}>
                <Plus size={16} /> New Reconciliation
              </button>
            </div>
          ) : (
            <div className="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Job ID</th>
                    <th>Client / Period</th>
                    <th>Mismatches</th>
                    <th>Escalations</th>
                    <th>Created</th>
                    <th>Status</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.slice(0, 10).map(j => {
                    const s = STATUS_BADGE[j.status] ?? STATUS_BADGE.pending;
                    return (
                      <tr key={j.id} style={{ cursor: "pointer" }} onClick={() => navigate(`/jobs/${j.id}`)}>
                        <td style={{ fontFamily: "monospace", fontSize: "0.75rem", color: "var(--text-muted)" }}>
                          {j.id?.slice(0, 8)}…
                        </td>
                        <td style={{ color: "var(--text-primary)", fontWeight: 500 }}>
                          {j.gstin ?? "—"}<br/>
                          <span style={{ fontSize: "0.75rem", color: "var(--text-muted)", fontWeight: 400 }}>{j.filing_period ?? ""}</span>
                        </td>
                        <td>{j.mismatch_count ?? 0}</td>
                        <td style={{ color: (j.escalation_count ?? 0) > 0 ? "var(--danger)" : "inherit" }}>
                          {j.escalation_count ?? 0}
                        </td>
                        <td>{fmt(j.created_at)}</td>
                        <td><span className={`badge ${s.cls}`}>{s.label}</span></td>
                        <td>
                          <button className="btn btn-ghost btn-sm" onClick={e => { e.stopPropagation(); navigate(`/jobs/${j.id}`); }}>
                            Open →
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Service health */}
        {health && (
          <div className="card">
            <div className="card-header"><h3>Service Health</h3></div>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
              {Object.entries(health.services ?? {}).map(([name, info]) => (
                <div key={name} style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                    <span className={`status-dot ${info.status}`} />
                    <span style={{ fontSize: "0.875rem", textTransform: "capitalize", color: "var(--text-primary)" }}>
                      {name}
                    </span>
                  </div>
                  <span className={`badge ${info.status === "up" ? "badge-success" : "badge-danger"}`}>
                    {info.status}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
