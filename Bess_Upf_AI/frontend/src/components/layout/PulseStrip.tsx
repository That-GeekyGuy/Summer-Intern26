import { useEffect, useState } from "react";
import { Credentials, queryInstant } from "../../api/client";
import { POLL_INTERVALS, PULSE_WINDOW_SEC } from "../../lib/constants";

interface PulsePoint { t: number; v: number; }

// Session capacity thresholds for pfcp_sessions_total.
// Sim baseline: ~1 M sessions (diurnal ±30 %) → 700 k – 1.3 M
// Spike scenario: 4 M sessions; capacity ceiling: 2 M per node.
const WARN_THRESHOLD = 1_500_000;  // 75 % of per-node capacity
const CRIT_THRESHOLD = 1_800_000;  // 90 % of per-node capacity

interface Props { creds: Credentials; }

export function PulseStrip({ creds }: Props) {
  const [points, setPoints] = useState<PulsePoint[]>([]);
  const [status, setStatus] = useState<"ok" | "warning" | "critical" | "unknown">("unknown");

  useEffect(() => {
    async function poll() {
      try {
        const sql = "SELECT upf_id as node, pfcp_sessions_total as value FROM bess_upf.upf_metrics WHERE ts >= (now() - toIntervalMinute(1)) ORDER BY ts DESC LIMIT 1";
        const resp = await queryInstant(creds, sql);
        if (resp.samples.length === 0) return;

        // Sum across all label-sets (multiple PFCP contexts) to get total.
        const total = resp.samples.reduce((sum, s) => sum + (s.value ?? s.Value ?? 0), 0);
        const now = Date.now();

        setPoints((prev) => {
          const cutoff = now - PULSE_WINDOW_SEC * 1000;
          return [...prev.filter((p) => p.t > cutoff), { t: now, v: total }];
        });
        setStatus(
          total > CRIT_THRESHOLD ? "critical" :
          total > WARN_THRESHOLD ? "warning" : "ok"
        );
      } catch {
        setStatus("unknown");
      }
    }

    poll();
    const id = setInterval(poll, POLL_INTERVALS.pulse);
    return () => clearInterval(id);
  }, [creds]);

  const color =
    status === "critical" ? "var(--signal-critical)" :
    status === "warning"  ? "var(--signal-warning)" :
    status === "ok"       ? "var(--signal-ok)" :
    "var(--signal-neutral)";

  // SVG sparkline — last PULSE_WINDOW_SEC seconds, no axes
  const W = 200, H = 32;
  let sparkPath = "";
  if (points.length >= 2) {
    const vals  = points.map((p) => p.v);
    const min   = Math.min(...vals);
    const max   = Math.max(...vals);
    const range = max - min || 1;
    const tMin  = points[0].t;
    const tSpan = (points[points.length - 1].t - tMin) || 1;
    sparkPath   = points
      .map((p, i) => {
        const x = ((p.t - tMin) / tSpan) * (W - 4) + 2;
        const y = H - 4 - ((p.v - min) / range) * (H - 8);
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  }

  return (
    <div
      title={`System pulse — pfcp_sessions_total · ${status}`}
      style={{
        height: 32,
        borderBottom: "1px solid var(--border)",
        position: "relative",
        overflow: "hidden",
        flexShrink: 0,
        animation: status !== "unknown" ? "pulse-glow 3s infinite" : "none",
      }}
    >
      {/* Tinted wash behind the sparkline */}
      <div style={{ position: "absolute", inset: 0, background: `linear-gradient(90deg, transparent, ${color}33)` }} />

      {/* Sparkline */}
      {sparkPath && (
        <svg width={W} height={H} style={{ position: "absolute", inset: 0 }} aria-hidden="true">
          <path
            d={sparkPath}
            fill="none"
            stroke={color}
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            opacity={1}
          />
        </svg>
      )}

      {/* Status dot — always in the top-right corner */}
      <div style={{
        position: "absolute", right: 6, top: "50%",
        transform: "translateY(-50%)",
        width: 6, height: 6,
        borderRadius: "50%",
        background: color,
        boxShadow: `0 0 4px ${color}`,
      }} />
    </div>
  );
}
