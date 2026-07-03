import { fmtSessions, fmtBytes, fmtDrops } from "../../lib/formatters";
import { motion } from "framer-motion";

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
    "var(--border)";

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
      ? (trend > 5 ? "var(--signal-critical)" : "var(--signal-ok)")
      : (trend > 0 ? "var(--signal-ok)" : "var(--signal-warning)");
  
  const trendBg = trend === undefined ? "transparent"
    : type === "drops"
      ? (trend > 5 ? "rgba(239, 68, 68, 0.1)" : "rgba(16, 185, 129, 0.1)")
      : (trend > 0 ? "rgba(16, 185, 129, 0.1)" : "rgba(245, 158, 11, 0.1)");

  return (
    <motion.div 
      whileHover={{ scale: 1.02, translateY: -2, boxShadow: "var(--shadow-elevated)", borderColor: "var(--border-focus)" }}
      whileTap={{ scale: 0.98 }}
      layout
      style={{
        background: "var(--bg-surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "24px",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        gap: 16,
        flex: 1,
        minWidth: 0,
        boxShadow: "var(--shadow-soft)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{
          fontSize: "var(--text-sm)",
          color: "var(--text-secondary)",
          fontWeight: 500,
        }}>
          {label}
        </div>
        {/* Status dot indicator instead of top border */}
        <div style={{
          width: 8, height: 8, borderRadius: "50%",
          background: statusColor,
          boxShadow: `0 0 8px ${statusColor}`,
        }} />
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

      {trend !== undefined && (
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ 
            background: trendBg,
            color: trendColor,
            padding: "2px 8px",
            borderRadius: 100,
            fontSize: "var(--text-2xs)",
            fontWeight: 600,
            fontFamily: "var(--font-mono)",
            display: "flex",
            alignItems: "center",
            gap: 4
          }}>
            {trendIcon} {Math.abs(trend).toFixed(1)}%
          </span>
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            vs 1h ago
          </span>
        </div>
      )}
    </motion.div>
  );
}
