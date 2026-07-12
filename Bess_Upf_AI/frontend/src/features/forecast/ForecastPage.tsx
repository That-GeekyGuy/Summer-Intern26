import { useMemo } from "react";
import { Credentials, fetchAnomalies, queryInstant, fetchIntervals, AnomalyEvent, IntervalsResponse } from "../../api/client";
import { useQuery, useQueries } from "@tanstack/react-query";
import { AreaChart, Area, ResponsiveContainer, Tooltip, XAxis } from "recharts";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { displayName, severityColor, fmtMetricVal } from "../../lib/metrics";
import { fmtEta } from "../../lib/formatters";
import { POLL_INTERVALS } from "../../lib/constants";

interface Props { creds: Credentials; }

// Always-on monitoring targets — displayed even when no alarm is active.
// capacities must match config/detection/rules.yml forecast.targets[*].capacity
//
// Calibrated to this dev cluster's actual traffic source (upf-sim, single
// node): BaseSessions=12,000, diurnal ±30%/jitter ±2% (~8.2k-15.9k normal),
// SpikeMultiplier=4x (~48k typical spike, ~64k worst-case diurnal-peak+spike).
// A production deployment with real fleet-scale traffic would need different
// values — these are sized so a triggered Scenario spike is visible and can
// realistically cross capacity, not sit at a fraction of a percent forever.
const FORECAST_METRICS = [
  {
    metric: "pfcp_sessions_total",
    label: "per node",
    sql: 'SELECT upf_id as node, pfcp_sessions_total as value FROM bess_upf.upf_metrics WHERE ts >= (now() - toIntervalMinute(1)) ORDER BY ts DESC LIMIT 1',
    capacity: 50_000,
    severity: "high",
  },
  {
    metric: "pfcp_sessions_total_cluster",
    label: "cluster",
    sql: 'SELECT sum(pfcp_sessions_total) as value FROM (SELECT upf_id, argMax(pfcp_sessions_total, ts) as pfcp_sessions_total FROM bess_upf.upf_metrics WHERE ts >= (now() - toIntervalMinute(1)) GROUP BY upf_id)',
    capacity: 65_000,
    severity: "critical",
  },
  {
    metric: "port_bytes_count",
    label: "N3 rx",
    sql: 'SELECT sum(port_bytes_N3_rx_rate) as value FROM (SELECT upf_id, argMax(port_bytes_N3_rx_rate, ts) as port_bytes_N3_rx_rate FROM bess_upf.upf_metrics WHERE ts >= (now() - toIntervalMinute(5)) GROUP BY upf_id)',
    capacity: 75_000_000,
    severity: "high",
  },
  {
    metric: "port_dropped_count",
    label: "all",
    sql: 'SELECT sum(port_dropped_N3_rx_rate + port_dropped_N6_rx_rate) as value FROM (SELECT upf_id, argMax(port_dropped_N3_rx_rate, ts) as port_dropped_N3_rx_rate, argMax(port_dropped_N6_rx_rate, ts) as port_dropped_N6_rx_rate FROM bess_upf.upf_metrics WHERE ts >= (now() - toIntervalMinute(2)) GROUP BY upf_id)',
    capacity: null,
    severity: "medium",
  },
] as const;

// Converts IntervalsResponse arrays into recharts data objects.
function toChartData(iv: IntervalsResponse | undefined) {
  if (!iv?.available || !iv.p50?.length) return null;
  return iv.p50.map((p50, i) => ({
    i,
    p10: iv.p10?.[i] ?? p50,
    p50,
    p90: iv.p90?.[i] ?? p50,
  }));
}

function IntervalsChart({ iv }: { iv: IntervalsResponse | undefined }) {
  const data = toChartData(iv);
  if (!data) return null;
  return (
    <div style={{ marginTop: 8, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
      <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", marginBottom: 4, fontFamily: "var(--font-mono)" }}>
        Chronos-2 · 80% prediction interval
      </div>
      <ResponsiveContainer width="100%" height={64}>
        <AreaChart data={data} margin={{ top: 2, right: 4, left: 4, bottom: 0 }}>
          <XAxis dataKey="i" hide />
          <Tooltip
            contentStyle={{ background: "var(--bg-elevated)", border: "1px solid var(--border)", fontSize: 10, padding: "4px 8px" }}
            formatter={(v: number, name: string) => [v.toExponential(2), name.toUpperCase()]}
            labelFormatter={() => ""}
          />
          {/* P90 area — upper bound */}
          <Area type="monotone" dataKey="p90" stroke="none" fill="#3b82f6" fillOpacity={0.15} legendType="none" />
          {/* P10 area — subtract lower band (bg color fill to create a band effect) */}
          <Area type="monotone" dataKey="p10" stroke="none" fill="var(--bg-surface)" fillOpacity={1} legendType="none" />
          {/* P50 median line */}
          <Area type="monotone" dataKey="p50" stroke="#3b82f6" strokeWidth={1.5} fill="none" dot={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function barColor(pct: number): string {
  if (pct >= 90) return "var(--signal-critical)";
  if (pct >= 70) return "var(--signal-warning)";
  return "var(--signal-info)";
}

export function ForecastPage({ creds }: Props) {
  const { setContext } = useAppStore();

  // Instant query per monitored metric — always reflects current VM state
  const metricQueries = useQueries({
    queries: FORECAST_METRICS.map(m => ({
      queryKey: ["forecast-live", m.metric],
      queryFn: () => queryInstant(creds, m.sql),
      refetchInterval: POLL_INTERVALS.forecast,
      retry: 1,
    })),
  });

  // DB predictive events — provide ETA and trend data when a breach is forecast
  const { data: dbData, isFetching: dbFetching } = useQuery({
    queryKey: ["predictions"],
    queryFn: () => fetchAnomalies(creds),
    refetchInterval: POLL_INTERVALS.forecast,
    placeholderData: (p) => p,
    retry: 1,
  });

  // Most-recent predictive event per metric_name (API returns DESC by created_at)
  const dbEvents = useMemo(() => {
    const map = new Map<string, AnomalyEvent>();
    for (const e of (dbData?.anomalies ?? []).filter(e => e.event_type === "predictive")) {
      if (!map.has(e.metric_name)) map.set(e.metric_name, e);
    }
    return map;
  }, [dbData]);

  // P10/P50/P90 forecast uncertainty intervals per metric, served by the
  // Chronos-2 (amazon/chronos-t5-small) sidecar. /api/v1/intervals takes the
  // metric key (not SQL) and builds its own bucketed ClickHouse context query.
  const intervalQueries = useQueries({
    queries: FORECAST_METRICS.map(m => ({
      queryKey: ["intervals", m.metric],
      queryFn: () => fetchIntervals(creds, m.metric),
      refetchInterval: POLL_INTERVALS.forecast,
      staleTime: POLL_INTERVALS.forecast / 2,
      retry: 0,
    })),
  });

  const isFetching = metricQueries.some(q => q.isFetching) || dbFetching;

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden", padding: 16, gap: 16 }}>
      {/* Bento Container */}
      <div style={{
        display: "flex", flexDirection: "column", flex: 1, overflow: "auto", padding: 32,
        background: "var(--bg-surface)",
        borderRadius: "var(--radius)",
        border: "1px solid var(--border)",
        boxShadow: "var(--shadow-soft)",
      }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 24 }}>
          <h2 style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xl)", fontWeight: 700, margin: 0, color: "var(--text-primary)" }}>
            {COPY.forecast.title}
          </h2>
          {isFetching && (
            <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>refreshing…</span>
          )}
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", gap: 24 }}>
          {(() => {
            const renderNow = Date.now(); // single timestamp for this render pass
            return FORECAST_METRICS.map((m, i) => {
              const q = metricQueries[i];
            const iv = intervalQueries[i]?.data;
            // Sum all returned samples (per-node metrics like pfcp_sessions_total return
            // one sample per UPF node; rate queries with sum() return a single sample).
            const samplesArr = q.data?.samples ?? [];
            const currentValue = samplesArr.length > 0
              ? samplesArr.reduce((acc, s) => acc + (s.value ?? s.Value ?? 0), 0)
              : null;
            const dbEvent = dbEvents.get(m.metric) ?? null;
            const capacity = m.capacity as number | null;

            const utilPct = capacity !== null && currentValue !== null
              ? Math.min(100, (currentValue / capacity) * 100)
              : null;

            // Trend %: how much the DB-projected value deviates from the current live value.
            // expected_value in a predictive event holds the extrapolated value at the
            // forecast horizon (e.g. 1h from now), so this is a genuine forward-looking %.
            const trendPct = dbEvent?.expected_value !== undefined && currentValue !== null && currentValue !== 0
              ? ((dbEvent.expected_value - currentValue) / Math.abs(currentValue)) * 100
              : null;

            const etaH = dbEvent?.predicted_crossing_time
              ? (dbEvent.predicted_crossing_time * 1000 - renderNow) / 3_600_000
              : -1;

            const alarming = dbEvent !== null;
            const bColor = utilPct !== null
              ? barColor(utilPct)
              : alarming ? severityColor(m.severity) : "var(--signal-info)";

            return (
              <div
                key={m.metric}
                onClick={() => alarming && dbEvent && setContext({ type: "event", event: dbEvent })}
                style={{
                  background: "var(--bg-base)",
                  border: "1px solid var(--border)",
                  borderRadius: 24,
                  padding: "24px",
                  width: 360,
                  cursor: alarming ? "pointer" : "default",
                  display: "flex",
                  flexDirection: "column",
                  gap: 16,
                  boxShadow: "var(--shadow-soft)",
                  position: "relative",
                  overflow: "hidden"
                }}
              >
                {alarming && (
                  <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 6, background: severityColor(m.severity) }} />
                )}
                
                {/* Header: metric name + % utilization */}
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", paddingLeft: alarming ? 8 : 0 }}>
                  <div>
                    <div style={{ fontSize: "var(--text-lg)", fontWeight: 600, color: "var(--text-primary)" }}>
                      {displayName(m.metric)}
                    </div>
                    <div style={{ fontSize: "var(--text-sm)", color: "var(--text-muted)", marginTop: 2 }}>
                      {m.label}
                    </div>
                  </div>
                  {utilPct !== null && (
                    <div style={{
                      fontSize: "var(--text-2xl)",
                      fontWeight: 700,
                      fontFamily: "var(--font-mono)",
                      color: bColor,
                      lineHeight: 1.1,
                    }}>
                      {utilPct.toFixed(1)}%
                    </div>
                  )}
                </div>

                {/* Utilization bar */}
                <div style={{ background: "var(--bg-subtle)", borderRadius: 100, height: 6, overflow: "hidden", marginLeft: alarming ? 8 : 0 }}>
                  <div style={{
                    height: "100%",
                    width: utilPct !== null
                      ? `${Math.max(2, utilPct).toFixed(0)}%`
                      : q.isLoading ? "5%" : "50%",
                    background: bColor,
                    borderRadius: 100,
                    transition: "width 0.3s ease",
                  }} />
                </div>

                {/* Now / Capacity */}
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: "var(--text-sm)", marginLeft: alarming ? 8 : 0 }}>
                  <span style={{ color: "var(--text-muted)" }}>
                    Now:{" "}
                    <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)", fontWeight: 500 }}>
                      {currentValue !== null
                        ? fmtMetricVal(m.metric, currentValue)
                        : q.isLoading ? "…" : "—"}
                    </span>
                  </span>
                  {capacity !== null && (
                    <span style={{ color: "var(--text-muted)" }}>
                      Capacity:{" "}
                      <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)", fontWeight: 500 }}>
                        {fmtMetricVal(m.metric, capacity)}
                      </span>
                    </span>
                  )}
                </div>

                {/* Projected value at horizon (from DB event) */}
                {dbEvent?.expected_value !== undefined && (
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: "var(--text-xs)", color: "var(--text-muted)", marginLeft: alarming ? 8 : 0 }}>
                    <span>Projected at {dbEvent.forecast_horizon ?? "1h"}:</span>
                    <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
                      {fmtMetricVal(m.metric, dbEvent.expected_value)}
                    </span>
                  </div>
                )}

                {/* ETA badge (only when breach is forecast) */}
                {dbEvent?.predicted_crossing_time && (
                  <div style={{
                    padding: "8px 12px",
                    background: "rgba(245, 158, 11, 0.1)",
                    borderRadius: 12,
                    fontSize: "var(--text-sm)",
                    color: "var(--signal-warning)",
                    fontFamily: "var(--font-mono)",
                    fontWeight: 500,
                    marginLeft: alarming ? 8 : 0
                  }}>
                    {COPY.forecast.etaLabel}: {fmtEta(dbEvent.predicted_crossing_time, renderNow)}
                  </div>
                )}

                {/* Plain-English summary */}
                <p style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", lineHeight: 1.6, margin: 0, marginLeft: alarming ? 8 : 0 }}>
                  {trendPct !== null
                    ? COPY.forecast.summary(displayName(m.metric), trendPct, etaH > 0 ? etaH : 0)
                    : `${displayName(m.metric)} is within normal operating range.`}
                </p>

                {/* Chronos-2 uncertainty bands */}
                <div style={{ marginLeft: alarming ? 8 : 0 }}>
                  <IntervalsChart iv={iv} />
                </div>
              </div>
            );
            });
          })()}
        </div>
      </div>
    </div>
  );
}
