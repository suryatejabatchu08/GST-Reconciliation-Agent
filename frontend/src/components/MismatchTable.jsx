// frontend/src/components/MismatchTable.jsx
// Displays mismatch rows grouped by severity with colour-coded badges.

const SEVERITY_CONFIG = {
  escalation: { label: "Escalation", cls: "badge-danger" },
  follow_up:  { label: "Follow Up",  cls: "badge-warning" },
  auto:       { label: "Auto",       cls: "badge-info" },
  default:    { label: "Unknown",    cls: "badge-default" },
};

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency", currency: "INR", maximumFractionDigits: 0,
});

export default function MismatchTable({ mismatches = [] }) {
  if (!mismatches.length) {
    return (
      <div className="empty-state">
        <p>No mismatches found for this job.</p>
      </div>
    );
  }

  return (
    <div className="table-wrapper">
      <table>
        <thead>
          <tr>
            <th>Supplier GSTIN</th>
            <th>Invoice #</th>
            <th>Mismatch Type</th>
            <th>Your Amount</th>
            <th>Portal Amount</th>
            <th>Difference</th>
            <th>Severity</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {mismatches.map((m) => {
            const sev = SEVERITY_CONFIG[m.severity] ?? SEVERITY_CONFIG.default;
            const diff = (m.our_amount ?? 0) - (m.portal_amount ?? 0);
            return (
              <tr key={m.id}>
                <td style={{ fontFamily: "monospace", fontSize: "0.8rem", color: "var(--text-primary)" }}>
                  {m.supplier_gstin ?? "—"}
                </td>
                <td style={{ color: "var(--text-primary)", fontWeight: 500 }}>
                  {m.invoice_number ?? "—"}
                </td>
                <td>
                  <span style={{ color: "var(--text-primary)", fontSize: "0.82rem" }}>
                    {m.mismatch_type?.replace(/_/g, " ") ?? "—"}
                  </span>
                </td>
                <td>{m.our_amount != null ? INR.format(m.our_amount) : "—"}</td>
                <td>{m.portal_amount != null ? INR.format(m.portal_amount) : "—"}</td>
                <td style={{ color: diff < 0 ? "var(--danger)" : diff > 0 ? "var(--warning)" : "var(--success)", fontWeight: 600 }}>
                  {INR.format(Math.abs(diff))}
                </td>
                <td>
                  <span className={`badge ${sev.cls}`}>{sev.label}</span>
                </td>
                <td style={{ fontSize: "0.8rem", color: "var(--info)" }}>
                  {m.recommended_action ?? "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
