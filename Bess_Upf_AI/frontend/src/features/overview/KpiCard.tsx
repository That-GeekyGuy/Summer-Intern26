import { fmtSessions, fmtBytes, fmtDrops } from "../../lib/formatters";

type KpiType = "sessions" | "bytes" | "drops" | "generic";

interface Props {
  label: string;
  value: number;
  type?: KpiType;
  trend?: number;  // percentage change
  status?: "ok" | "warning" | "critical" | "unknown";
  loading?: boolean;
}

export function KpiCard({ label, value, type = "generic", trend, status = "unknown", loading }: Props) {
  const statusColor =
    status === "critical" ? "var(--signal-critical)" :
    status === "warning"  ? "var(--signal-warning)" :
    status === "ok"       ? "var(--signal-ok)" :
    "transparent";

  function formatted() {
    if (loading || isNaN(value)) return "—";
    switch (type) {
      case "sessions": return fmtSessions(value);
      case "bytes":    return fmtBytes(value);
      case "drops":    return fmtDrops(value);
      default:         return value.toLocaleString();
    }
  }

  const trendIcon = trend === undefined ? null
    : trend > 1 ? "↑" : trend < -1 ? "↓" : "→";
  const trendColor = trend === undefined ? "var(--text-muted)"
    : type === "drops"
      ? (trend > 5 ? "var(--signal-warning)" : "var(--text-secondary)")
      : "var(--text-secondary)";

  return (
    <div style={{
      background: "var(--bg-surface)",
      border: "1px solid var(--border)",
      borderLeft: `3px solid ${statusColor}`,
      borderRadius: "var(--radius)",
      padding: "16px 20px",
      display: "flex",
      flexDirection: "column",
      gap: 8,
      flex: 1,
      minWidth: 0,
    }}>
      <div style={{
        fontSize: "var(--text-xs)",
        color: "var(--text-muted)",
        textTransform: "uppercase",
        letterSpacing: "0.5px",
        fontFamily: "var(--font-mono)",
      }}>
        {label}
      </div>
      <div style={{
        fontSize: "var(--text-2xl)",
        fontFamily: "var(--font-mono)",
        fontWeight: 700,
        color: loading ? "var(--text-muted)" : "var(--text-primary)",
        lineHeight: 1,
      }}>
        {formatted()}
      </div>
      {trendIcon && trend !== undefined && (
        <div style={{ fontSize: "var(--text-xs)", color: trendColor }}>
          {trendIcon} {Math.abs(trend).toFixed(1)}% vs 1h ago
        </div>
      )}
    </div>
  );
}
