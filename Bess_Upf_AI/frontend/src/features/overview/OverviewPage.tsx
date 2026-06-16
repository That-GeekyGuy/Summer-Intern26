import { Credentials, fetchAnomalies, queryInstant, AnomalyEvent } from "../../api/client";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { displayName, formatLabels, severityColor } from "../../lib/metrics";
import { fmtRelative, fmtEta } from "../../lib/formatters";
import { KpiCard } from "./KpiCard";
import { useQuery } from "@tanstack/react-query";
import { POLL_INTERVALS } from "../../lib/constants";

interface Props { creds: Credentials; }

// Fetches a single instant query and returns the sum of all returned samples.
// Returns NaN while loading or on error so KpiCard shows "—".
function useKpi(creds: Credentials, expr: string) {
  const { data, isLoading } = useQuery({
    queryKey: ["kpi", expr],
    queryFn: () => queryInstant(creds, expr),
    refetchInterval: 15_000,
    staleTime: 10_000,
    retry: 1,
  });
  const value = (data?.samples ?? []).reduce((s, x) => s + x.value, 0);
  return { value: data ? value : NaN, loading: isLoading };
}

// Derive a simple ok/warning/critical status from a value vs a threshold.
function kpiStatus(value: number, warnAt: number, critAt: number): "ok" | "warning" | "critical" | "unknown" {
  if (isNaN(value)) return "unknown";
  if (value >= critAt) return "critical";
  if (value >= warnAt) return "warning";
  return "ok";
}

export function OverviewPage({ creds }: Props) {
  const { setContext } = useAppStore();

  // ── Live KPI data from VictoriaMetrics ──────────────────────────────────
  const sessions = useKpi(creds, "sum(pfcp_sessions_total)");
  const n3rx     = useKpi(creds, 'sum(rate(port_bytes_count{dir="rx",iface="N3"}[1m]))');
  const n6tx     = useKpi(creds, 'sum(rate(port_bytes_count{dir="tx",iface="N6"}[1m]))');
  const drops    = useKpi(creds, "sum(rate(port_dropped_count[1m]))");

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
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>

      {/* ── KPI row ──────────────────────────────────────────────────────── */}
      <div style={{ display: "flex", gap: 12, padding: "16px 20px", flexShrink: 0, borderBottom: "1px solid var(--border)" }}>
        <KpiCard
          label={COPY.overview.kpi.sessions}
          value={sessions.value}
          type="sessions"
          loading={sessions.loading}
          status={kpiStatus(sessions.value, 15_000, 18_000)}
        />
        <KpiCard
          label={COPY.overview.kpi.n3Rx}
          value={n3rx.value}
          type="bytes"
          loading={n3rx.loading}
          status={kpiStatus(n3rx.value, 700_000_000, 900_000_000)}
        />
        <KpiCard
          label={COPY.overview.kpi.n6Tx}
          value={n6tx.value}
          type="bytes"
          loading={n6tx.loading}
          status={kpiStatus(n6tx.value, 700_000_000, 900_000_000)}
        />
        <KpiCard
          label={COPY.overview.kpi.drops}
          value={drops.value}
          type="drops"
          loading={drops.loading}
          status={kpiStatus(drops.value, 100, 500)}
        />
      </div>

      {/* ── Middle: events + forecast ────────────────────────────────────── */}
      <div style={{ display: "flex", flex: 1, overflow: "hidden", borderBottom: "1px solid var(--border)" }}>

        {/* Events feed — 60% */}
        <div style={{ flex: "0 0 60%", display: "flex", flexDirection: "column", overflow: "hidden", borderRight: "1px solid var(--border)" }}>
          <div style={{
            padding: "10px 16px", borderBottom: "1px solid var(--border)",
            fontSize: "var(--text-xs)", color: "var(--text-muted)",
            fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.5px", flexShrink: 0,
          }}>
            {COPY.overview.events}
          </div>
          <div style={{ flex: 1, overflowY: "auto" }}>
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
              <button
                key={i}
                onClick={() => handleEventClick(e)}
                style={{
                  width: "100%", textAlign: "left", background: "none", border: "none",
                  borderLeft: `3px solid ${severityColor(e.severity)}`,
                  borderBottom: "1px solid var(--border)",
                  padding: "10px 14px", cursor: "pointer",
                  display: "flex", flexDirection: "column", gap: 4,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8, justifyContent: "space-between" }}>
                  <span style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)", fontWeight: 500 }}>
                    {displayName(e.metric_name)}
                  </span>
                  <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                    {fmtRelative(e.timestamp)}
                  </span>
                </div>
                <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
                  {formatLabels(e.labels)} · {e.severity}
                  {e.event_type === "predictive" && e.predicted_crossing_time && (
                    <span style={{ marginLeft: 8, color: "var(--signal-warning)", fontSize: "var(--text-2xs)" }}>
                      ⏱ {fmtEta(e.predicted_crossing_time)}
                    </span>
                  )}
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* Forecast summary — 40% */}
        <div style={{ flex: "0 0 40%", display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <div style={{
            padding: "10px 16px", borderBottom: "1px solid var(--border)",
            fontSize: "var(--text-xs)", color: "var(--text-muted)",
            fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.5px", flexShrink: 0,
          }}>
            {COPY.overview.forecast}
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
            {events.filter(e => e.event_type === "predictive").slice(0, 3).map((e, i) => (
              <div
                key={i}
                onClick={() => handleEventClick(e)}
                style={{
                  background: "var(--bg-elevated)", border: "1px solid var(--border)",
                  borderRadius: "var(--radius)", padding: "10px 12px", cursor: "pointer",
                }}
              >
                <div style={{ fontSize: "var(--text-sm)", fontWeight: 500, marginBottom: 4 }}>
                  {displayName(e.metric_name)}
                </div>
                {e.predicted_crossing_time && (
                  <div style={{ fontSize: "var(--text-xs)", color: "var(--signal-warning)" }}>
                    ETA to capacity: {fmtEta(e.predicted_crossing_time)}
                  </div>
                )}
                <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginTop: 2 }}>
                  Current: {e.observed_value.toFixed(0)} · Threshold: {(e.expected_value ?? 0).toFixed(0)}
                </div>
              </div>
            ))}
            {events.filter(e => e.event_type === "predictive").length === 0 && (
              <p style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
                No active forecasts — all trends within safe range.
              </p>
            )}
          </div>
        </div>
      </div>

      {/* ── System health strip ──────────────────────────────────────────── */}
      <div style={{
        padding: "10px 20px", flexShrink: 0,
        display: "flex", gap: 24, alignItems: "center", overflowX: "auto",
      }}>
        {[
          COPY.overview.services.prometheus,
          COPY.overview.services.vm,
          COPY.overview.services.detection,
          COPY.overview.services.llm,
          COPY.overview.services.sim,
        ].map((svc) => (
          <div key={svc} style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
            <div style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--signal-ok)" }} />
            <span style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>{svc}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
