// frontend/src/components/StatCard.jsx
import { TrendingUp, TrendingDown } from "lucide-react";

export default function StatCard({ icon: Icon, label, value, sub, trend, color = "accent" }) {
  const colorMap = {
    accent:  { bg: "var(--accent-glow)",   color: "var(--accent-light)" },
    success: { bg: "var(--success-bg)",    color: "var(--success)" },
    warning: { bg: "var(--warning-bg)",    color: "var(--warning)" },
    danger:  { bg: "var(--danger-bg)",     color: "var(--danger)" },
    info:    { bg: "var(--info-bg)",       color: "var(--info)" },
  };

  const { bg, color: iconColor } = colorMap[color] ?? colorMap.accent;

  return (
    <div className="card" style={{ position: "relative", overflow: "hidden" }}>
      {/* Subtle glow top-right */}
      <div style={{
        position: "absolute", top: -20, right: -20,
        width: 80, height: 80,
        background: bg, borderRadius: "50%",
        filter: "blur(24px)", pointerEvents: "none",
      }} />

      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", position: "relative" }}>
        <div>
          <p style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "0.5rem" }}>
            {label}
          </p>
          <div style={{ fontSize: "2rem", fontWeight: 800, color: "var(--text-primary)", lineHeight: 1, marginBottom: "0.35rem" }}>
            {value}
          </div>
          {sub && (
            <p style={{ fontSize: "0.8rem", color: "var(--text-muted)", margin: 0, display: "flex", alignItems: "center", gap: "0.3rem" }}>
              {trend !== undefined && (
                trend >= 0
                  ? <TrendingUp size={12} color="var(--success)" />
                  : <TrendingDown size={12} color="var(--danger)" />
              )}
              {sub}
            </p>
          )}
        </div>
        {Icon && (
          <div style={{
            width: 44, height: 44, borderRadius: "var(--radius-md)",
            background: bg, display: "flex", alignItems: "center",
            justifyContent: "center", flexShrink: 0,
          }}>
            <Icon size={20} color={iconColor} />
          </div>
        )}
      </div>
    </div>
  );
}
