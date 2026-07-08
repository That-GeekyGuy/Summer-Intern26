import { Credentials, fetchAnomalies, AnomalyEvent, fetchPulse, fetchHealth } from "../../api/client";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { displayName, formatLabels, severityColor, fmtMetricVal } from "../../lib/metrics";
import { fmtRelative, fmtEta } from "../../lib/formatters";
import { KpiCard } from "./KpiCard";
import { useQuery } from "@tanstack/react-query";
import { POLL_INTERVALS } from "../../lib/constants";
import { motion, Variants } from "framer-motion";

const containerVariants: Variants = {
  hidden: { opacity: 0 },
  show: {
    opacity: 1,
    transition: { staggerChildren: 0.1 }
  }
};

const itemVariants: Variants = {
  hidden: { opacity: 0, y: 20 },
  show: { opacity: 1, y: 0, transition: { type: "spring", stiffness: 300, damping: 24 } }
};

interface Props { creds: Credentials; }

// Derive a simple ok/warning/critical status from a value vs a threshold.
function kpiStatus(value: number, warnAt: number, critAt: number): "ok" | "warning" | "critical" | "unknown" {
  if (isNaN(value)) return "unknown";
  if (value >= critAt) return "critical";
  if (value >= warnAt) return "warning";
  return "ok";
}

export function OverviewPage({ creds }: Props) {
  const { setContext } = useAppStore();

  // ── Live KPI data from VictoriaMetrics (Batched Pulse) ────────────────
  const { data: pulseData, isLoading: pulseLoading } = useQuery({
    queryKey: ["pulse"],
    queryFn: () => fetchPulse(creds),
    refetchInterval: POLL_INTERVALS.forecast, // 15 s
    staleTime: POLL_INTERVALS.forecast - 2_000,
    retry: 1,
  });

  const sessions = { value: pulseData?.sessions ?? NaN, loading: pulseLoading };
  const n3rx = { value: pulseData?.n3rx ?? NaN, loading: pulseLoading };
  const n6tx = { value: pulseData?.n6tx ?? NaN, loading: pulseLoading };
  const drops = { value: pulseData?.drops ?? NaN, loading: pulseLoading };

  // ── System Health ────────────────────────────────────────────────────────
  const { data: healthData } = useQuery({
    queryKey: ["health"],
    queryFn: () => fetchHealth(creds),
    refetchInterval: 10_000,
    retry: 1,
  });



  // ── Anomaly feed ─────────────────────────────────────────────────────────
  const { data: anomData, error: anomError } = useQuery({
    queryKey: ["anomalies", "overview"],
    queryFn: () => fetchAnomalies(creds),
    refetchInterval: POLL_INTERVALS.anomalies,
    placeholderData: (p) => p,
    retry: 1,
  });

  const events    = anomData?.anomalies ?? [];
  const topEvents = events.slice(0, 10);

  function handleEventClick(e: AnomalyEvent) {
    setContext({ type: "event", event: e });
  }

  return (
    <motion.div 
      variants={containerVariants}
      initial="hidden"
      animate="show"
      style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", padding: 16, gap: 16 }}
    >



      {/* ── KPI row ──────────────────────────────────────────────────────── */}
      <motion.div variants={itemVariants} style={{ display: "flex", gap: 16, flexShrink: 0 }}>
        <KpiCard
          label={COPY.overview.kpi.sessions}
          value={sessions.value}
          type="sessions"
          loading={sessions.loading}
          status={kpiStatus(sessions.value, 1_500_000, 1_800_000)}  // warn at 75 %, crit at 90 % of 2M capacity
        />
        <KpiCard
          label={COPY.overview.kpi.n3Rx}
          value={n3rx.value}
          type="bytes"
          loading={n3rx.loading}
          status={kpiStatus(n3rx.value, 7_500_000_000, 9_000_000_000)}  // warn at 7.5 GB/s, crit at 9 GB/s (10 GB/s link)
        />
        <KpiCard
          label={COPY.overview.kpi.n6Tx}
          value={n6tx.value}
          type="bytes"
          loading={n6tx.loading}
          status={kpiStatus(n6tx.value, 7_500_000_000, 9_000_000_000)}
        />
        <KpiCard
          label={COPY.overview.kpi.drops}
          value={drops.value}
          type="drops"
          loading={drops.loading}
          status={kpiStatus(drops.value, 100, 500)}  // warn at 100/s, crit at 500/s
        />
      </motion.div>

      {/* ── Middle: events + forecast ────────────────────────────────────── */}
      <motion.div variants={itemVariants} style={{ display: "flex", flex: 1, gap: 16, overflow: "hidden" }}>

        {/* Events feed — 60% */}
        <div style={{ flex: "0 0 60%", display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--bg-surface)", borderRadius: "var(--radius)", border: "1px solid var(--border)", boxShadow: "var(--shadow-soft)" }}>
          <div style={{
            padding: "16px 20px", borderBottom: "1px solid var(--border)",
            fontSize: "var(--text-sm)", color: "var(--text-primary)",
            fontWeight: 600, flexShrink: 0,
          }}>
            {COPY.overview.events}
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: 16 }}>
            {anomError ? (
              <div style={{ padding: "16px", color: "var(--signal-critical)", fontSize: "var(--text-sm)" }}>
                Detection service unavailable — check system health.
                <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-muted)", marginTop: 4 }}>
                  {String(anomError)}
                </div>
              </div>
            ) : topEvents.length === 0 ? (
              <div style={{ padding: "24px 16px", color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
                {COPY.overview.noEvents}
              </div>
            ) : topEvents.map((e, i) => (
              <motion.button
                whileHover={{ scale: 1.01, background: "var(--bg-elevated)" }}
                whileTap={{ scale: 0.99 }}
                key={i}
                onClick={() => handleEventClick(e)}
                style={{
                  width: "100%", textAlign: "left", background: "var(--bg-surface)", border: "1px solid var(--border)",
                  borderRadius: 16,
                  marginBottom: 12,
                  padding: "16px", cursor: "pointer",
                  display: "flex", flexDirection: "column", gap: 8,
                  position: "relative", overflow: "hidden"
                }}
              >
                {/* Severity indicator bar */}
                <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 4, background: severityColor(e.severity) }} />
                
                <div style={{ display: "flex", alignItems: "center", gap: 8, justifyContent: "space-between", paddingLeft: 8 }}>
                  <span style={{ fontSize: "var(--text-base)", color: "var(--text-primary)", fontWeight: 600 }}>
                    {displayName(e.metric_name)}
                  </span>
                  <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)", background: "var(--bg-elevated)", padding: "4px 8px", borderRadius: 100 }}>
                    {fmtRelative(e.timestamp)}
                  </span>
                </div>
                <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", paddingLeft: 8 }}>
                  <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>{formatLabels(e.labels)}</span> · {e.severity}
                  {e.event_type === "predictive" && e.predicted_crossing_time && (
                    <span style={{ marginLeft: 8, color: "var(--signal-warning)", fontSize: "var(--text-xs)", background: "rgba(245, 158, 11, 0.1)", padding: "2px 8px", borderRadius: 100 }}>
                      ⏱ ETA {fmtEta(e.predicted_crossing_time)}
                    </span>
                  )}
                </div>
              </motion.button>
            ))}
          </div>
        </div>

        {/* Forecast summary — 40% */}
        <div style={{ flex: "1", display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--bg-surface)", borderRadius: "var(--radius)", border: "1px solid var(--border)", boxShadow: "var(--shadow-soft)" }}>
          <div style={{
            padding: "16px 20px", borderBottom: "1px solid var(--border)",
            fontSize: "var(--text-sm)", color: "var(--text-primary)",
            fontWeight: 600, flexShrink: 0,
          }}>
            {COPY.overview.forecast}
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
            {events.filter(e => e.event_type === "predictive").slice(0, 3).map((e, i) => (
              <motion.div
                whileHover={{ scale: 1.02 }}
                key={i}
                onClick={() => handleEventClick(e)}
                style={{
                  background: "var(--bg-elevated)", border: "1px solid var(--border)",
                  borderRadius: 16, padding: "16px", cursor: "pointer",
                }}
              >
                <div style={{ fontSize: "var(--text-base)", fontWeight: 600, marginBottom: 8, color: "var(--text-primary)" }}>
                  {displayName(e.metric_name)}
                </div>
                {e.predicted_crossing_time && (
                  <div style={{ fontSize: "var(--text-sm)", color: "var(--signal-warning)", fontWeight: 500, marginBottom: 8 }}>
                    ETA to capacity: {fmtEta(e.predicted_crossing_time)}
                  </div>
                )}
                <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", display: "flex", gap: 12 }}>
                  <span>Current: {fmtMetricVal(e.metric_name, e.observed_value)}</span>
                  <span>Threshold: {fmtMetricVal(e.metric_name, e.expected_value ?? 0)}</span>
                </div>
              </motion.div>
            ))}
            {events.filter(e => e.event_type === "predictive").length === 0 && (
              <div style={{ padding: 24, textAlign: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)", border: "1px dashed var(--border)", borderRadius: 16 }}>
                No active forecasts — all trends within safe range.
              </div>
            )}
          </div>
        </div>
      </motion.div>

      {/* ── System health strip ────────────────────────────────────────────────────── */}
      <motion.div variants={itemVariants} style={{
        padding: "12px 20px", flexShrink: 0,
        display: "flex", gap: 24, alignItems: "center", overflowX: "auto",
        background: "var(--bg-surface)",
        borderRadius: 100,
        border: "1px solid var(--border)",
        boxShadow: "var(--shadow-soft)",
      }}>
        {[
          { key: "prometheus", label: COPY.overview.services.prometheus },
          { key: "vm", label: COPY.overview.services.vm },
          { key: "detection", label: COPY.overview.services.detection },
          { key: "llm", label: COPY.overview.services.llm },
          { key: "sim", label: COPY.overview.services.sim },
        ].map((svc) => {
          const status = healthData?.[svc.key] ?? "unknown";
          const color = status === "ok" ? "var(--signal-ok)" : status === "down" ? "var(--signal-critical)" : "var(--text-muted)";
          return (
          <div key={svc.key} style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
            <div style={{ width: 8, height: 8, borderRadius: "50%", background: color, boxShadow: `0 0 8px ${color}` }} />
            <span style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", fontWeight: 500 }}>{svc.label}</span>
          </div>
        )})}
        <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", marginLeft: "auto", fontFamily: "var(--font-mono)" }}>
          {new Date().toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC")}
        </span>
      </motion.div>
    </motion.div>
  );
}
