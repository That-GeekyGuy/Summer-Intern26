import { useMemo, useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Credentials, fetchAnomalies, queryInstant } from "../../api/client";
import { POLL_INTERVALS } from "../../lib/constants";

interface Props { creds: Credentials; }

type Regime = "low" | "normal" | "peak" | "surge";

const REGIME_COLOR: Record<Regime, string> = {
  low:    "#4a90d9",
  normal: "#27ae60",
  peak:   "#e67e22",
  surge:  "#e74c3c",
};

const REGIME_BG: Record<Regime, string> = {
  low:    "rgba(74,144,217,0.15)",
  normal: "rgba(39,174,96,0.15)",
  peak:   "rgba(230,126,34,0.18)",
  surge:  "rgba(231,76,60,0.18)",
};

function regimeLabel(r: Regime): string {
  return { low: "Quiet", normal: "Normal", peak: "Peak", surge: "Surge" }[r] ?? r;
}

function fmtHour(h: number): string {
  return `${String(h).padStart(2, "0")}:00`;
}

interface HourlyStat {
  hour: number;
  regime: Regime;
  value: number;
}

function HeatMapBar({ stat }: { stat: HourlyStat }) {
  const color = REGIME_COLOR[stat.regime] ?? "#888";
  const bg    = REGIME_BG[stat.regime] ?? "rgba(136,136,136,0.1)";
  return (
    <div title={`${fmtHour(stat.hour)}: ${regimeLabel(stat.regime)} (value: ${stat.value.toFixed(0)})`}
      style={{
        flex: 1,
        height: 48,
        background: bg,
        border: `1px solid ${color}`,
        borderRadius: 3,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        cursor: "default",
        transition: "transform 0.1s",
        fontSize: 9,
        color,
        fontFamily: "var(--font-mono)",
        gap: 2,
      }}
    >
      <span style={{ fontSize: 8, opacity: 0.7 }}>{stat.hour}</span>
      <span style={{ fontWeight: 700, fontSize: 9 }}>{regimeLabel(stat.regime).slice(0, 1)}</span>
    </div>
  );
}

function CalendarCard() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(timer);
  }, []);

  const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const dayName = days[now.getDay()];
  const isWeekend = now.getDay() === 0 || now.getDay() === 6;
  const weekOfMonth = Math.ceil(now.getDate() / 7);

  return (
    <div style={{
      background: "var(--bg-surface)",
      border: "1px solid var(--border)",
      borderRadius: 6,
      padding: "14px 18px",
      display: "flex",
      flexDirection: "column",
      gap: 10,
    }}>
      <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)" }}>
        Calendar Context
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: "var(--text-sm)" }}>
        <Row label="Day"        value={`${dayName}, ${fmtHour(now.getHours())} Local`} />
        <Row label="Type"       value={isWeekend ? "Weekend" : "Weekday"} />
        <Row label="Week"       value={`Week ${weekOfMonth} of month`} />
      </div>
    </div>
  );
}

// ---- Channel co-activation matrix (proxy correlation from ML anomaly scores) ----

const SHORT_LABELS: Record<string, string> = {
  port_bytes_N3_rx_rate: "N3↓B", port_bytes_N6_tx_rate: "N6↑B",
  port_pkts_N3_rx_rate:  "N3↓P", port_dropped_N3_rx_rate: "N3↓D",
  port_dropped_N6_rx_rate: "N6↓D", pfcp_sessions_total: "PFCP",
  pfcp_session_setup_rate: "SuR", dl_forwarding_efficiency: "DL↑E",
  dl_throughput_efficiency: "DL↑T", drop_rate_percentage: "Drop%",
};
function shortLabel(k: string) { return SHORT_LABELS[k] ?? k.slice(0, 5); }

function heatColor(v: number): string {
  const r = Math.round(59  + v * (239 - 59));
  const g = Math.round(130 + v * (68  - 130));
  const b = Math.round(246 + v * (68  - 246));
  return `rgba(${r},${g},${b},${0.15 + v * 0.7})`;
}

function CorrelationMatrix({ channels, matrix }: { channels: string[]; matrix: number[][] }) {
  const n = channels.length;
  if (n < 2) return null;
  return (
    <div style={{ background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 6, padding: "14px 18px" }}>
      <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: 12 }}>
        Channel Co-Activation Matrix
        <span style={{ marginLeft: 10, fontStyle: "italic", fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>
          (derived from ML anomaly channel scores — darker = co-activate more often)
        </span>
      </div>
      <div style={{
        display: "grid",
        gridTemplateColumns: `56px repeat(${n}, 1fr)`,
        gap: 1,
        fontSize: 9,
        fontFamily: "var(--font-mono)",
      }}>
        {/* Top header row */}
        <div />
        {channels.map(ch => (
          <div key={ch} style={{ textAlign: "center", color: "var(--text-muted)", padding: "2px 0", overflow: "hidden", textOverflow: "ellipsis" }}>
            {shortLabel(ch)}
          </div>
        ))}
        {/* Data rows */}
        {channels.map((rowCh, r) => (
          <>
            <div key={`lbl-${rowCh}`} style={{ color: "var(--text-muted)", paddingRight: 6, textAlign: "right", display: "flex", alignItems: "center", justifyContent: "flex-end" }}>
              {shortLabel(rowCh)}
            </div>
            {matrix[r].map((v, c) => (
              <div key={c} title={`${shortLabel(rowCh)} × ${shortLabel(channels[c])}: ${v.toFixed(2)}`}
                style={{
                  background: heatColor(v),
                  borderRadius: 2,
                  height: 22,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: v > 0.5 ? "var(--text-primary)" : "transparent",
                  fontSize: 8,
                }}
              >
                {v > 0.5 ? v.toFixed(1) : ""}
              </div>
            ))}
          </>
        ))}
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", gap: 8 }}>
      <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", minWidth: 60 }}>{label}</span>
      <span style={{ color: "var(--text-primary)", fontSize: "var(--text-xs)" }}>{value}</span>
    </div>
  );
}

export function InsightsPage({ creds }: Props) {
  // Fetch recent ML anomaly events to derive channel co-activation matrix
  const { data: anomalyData } = useQuery({
    queryKey: ["anomalies-insights"],
    queryFn: () => fetchAnomalies(creds),
    refetchInterval: POLL_INTERVALS.anomalies,
    staleTime: POLL_INTERVALS.anomalies / 2,
    retry: 1,
  });

  const { data: trafficData, isLoading: trafficLoading } = useQuery({
    queryKey: ["traffic-regimes"],
    queryFn: async () => {
      // Query the last 24 hours of pfcp_sessions_total
      const sql = "SELECT toHour(ts) as hr, avg(pfcp_sessions_total) as val FROM bess_upf.upf_metrics WHERE ts >= (now() - toIntervalHour(24)) GROUP BY hr ORDER BY hr ASC";
      const res = await queryInstant(creds, sql);
      
      const stats: HourlyStat[] = [];
      const values = res.samples.map(s => s.value || s.Value || 0);
      if (values.length === 0) return [];
      
      const sorted = [...values].sort((a,b) => a-b);
      const p15 = sorted[Math.floor(sorted.length * 0.15)];
      const p75 = sorted[Math.floor(sorted.length * 0.75)];
      const p95 = sorted[Math.floor(sorted.length * 0.95)];

      // Make sure we have 24 hours represented
      for (let i = 0; i < 24; i++) {
        // Find if we have data for this hour in the result set
        let val = 0;
        for (const s of res.samples) {
           const lbls = s.labels || s.Labels || {};
           if (parseInt(lbls.hr) === i) {
             val = s.value || s.Value || 0;
           }
        }
        let regime: Regime = "normal";
        if (val < p15) regime = "low";
        else if (val > p95) regime = "surge";
        else if (val > p75) regime = "peak";

        stats.push({ hour: i, regime, value: val });
      }

      return stats;
    },
    refetchInterval: POLL_INTERVALS.forecast,
  });

  const { channels, matrix } = useMemo(() => {
    const mlEvents = (anomalyData?.anomalies ?? []).filter(e => e.event_type === "ml" && e.feature_contributions);
    if (mlEvents.length < 2) return { channels: [], matrix: [] as number[][] };

    const chSet = new Set<string>();
    const vectors: Record<string, number>[] = [];
    for (const ev of mlEvents) {
      try {
        const raw = JSON.parse(ev.feature_contributions!);
        const scores: Record<string, number> = Array.isArray(raw)
          ? Object.fromEntries((raw as { name: string; importance: number }[]).map(x => [x.name, x.importance]))
          : raw as Record<string, number>;
        Object.keys(scores).forEach(k => chSet.add(k));
        vectors.push(scores);
      } catch { /* skip */ }
    }

    const chs = Array.from(chSet).slice(0, 10);
    if (chs.length < 2) return { channels: [], matrix: [] as number[][] };

    const vecs = vectors.map(sc => chs.map(ch => sc[ch] ?? 0));
    const n = chs.length;
    const mx: number[][] = Array.from({ length: n }, () => Array(n).fill(0));
    for (const vec of vecs) {
      const norm = Math.sqrt(vec.reduce((s, v) => s + v * v, 0)) || 1;
      const nvec = vec.map(v => v / norm);
      for (let r = 0; r < n; r++) {
        for (let c = 0; c < n; c++) {
          mx[r][c] += nvec[r] * nvec[c];
        }
      }
    }
    const maxOff = Math.max(...mx.flatMap((row, r) => row.filter((_, c) => c !== r))) || 1;
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        mx[r][c] = r === c ? 1 : Math.min(1, mx[r][c] / maxOff);
      }
    }
    return { channels: chs, matrix: mx };
  }, [anomalyData]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "auto", padding: "16px 20px", gap: 20 }}>
      {/* Page header */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexShrink: 0 }}>
        <h2 style={{ margin: 0, fontSize: "var(--text-lg)", fontWeight: 600, color: "var(--text-primary)" }}>
          Temporal Insights
        </h2>
        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
          Local Regime Detection · Calendar Awareness · ML Features
        </span>
      </div>

      {/* Two-column layout: heatmap + calendar */}
      <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
        {/* 24-hour regime heatmap */}
        <div style={{
          flex: "1 1 480px",
          background: "var(--bg-surface)",
          border: "1px solid var(--border)",
          borderRadius: 6,
          padding: "14px 18px",
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}>
          <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)" }}>
            24-Hour Traffic Regime Heatmap
          </div>

          {trafficLoading && (
            <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>Loading…</div>
          )}

          {trafficData && (
            <>
              {/* Regime bar strip */}
              <div style={{ display: "flex", gap: 2, alignItems: "stretch", height: 48 }}>
                {trafficData.map(stat => (
                  <HeatMapBar key={stat.hour} stat={stat} />
                ))}
              </div>

              {/* Hour labels (every 3h for readability) */}
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "var(--text-muted)", fontFamily: "var(--font-mono)", padding: "0 1px" }}>
                {[0, 3, 6, 9, 12, 15, 18, 21].map(h => (
                  <span key={h}>{fmtHour(h)}</span>
                ))}
              </div>

              {/* Legend */}
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 4 }}>
                {(["low", "normal", "peak", "surge"] as Regime[]).map(r => (
                  <div key={r} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: "var(--text-xs)" }}>
                    <div style={{ width: 10, height: 10, borderRadius: 2, background: REGIME_COLOR[r] }} />
                    <span style={{ color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>{regimeLabel(r)}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        {/* Calendar card */}
        <div style={{ flex: "0 0 260px" }}>
          <CalendarCard />
        </div>
      </div>

      {/* Channel co-activation matrix */}
      {channels.length >= 2 && <CorrelationMatrix channels={channels} matrix={matrix} />}

      {/* Regime interpretation guide */}
      <div style={{
        background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 6,
        padding: "14px 18px",
      }}>
        <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: 12 }}>
          Regime Label Interpretation
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12 }}>
          {[
            { r: "low" as Regime,    pct: "< 15th pct",   desc: "Below normal for this hour. Equipment may be underutilised." },
            { r: "normal" as Regime, pct: "15–75th pct",  desc: "Within expected range. No action needed." },
            { r: "peak" as Regime,   pct: "75–95th pct",  desc: "Elevated but within seasonal bounds (e.g. expected busy hour)." },
            { r: "surge" as Regime,  pct: "> 95th pct",   desc: "Above seasonal expectation. Investigate if sustained." },
          ].map(({ r, pct, desc }) => (
            <div key={r} style={{
              background: REGIME_BG[r], border: `1px solid ${REGIME_COLOR[r]}`,
              borderRadius: 4, padding: "8px 12px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                <span style={{ fontWeight: 700, color: REGIME_COLOR[r], fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", textTransform: "uppercase" }}>
                  {regimeLabel(r)}
                </span>
                <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {pct}
                </span>
              </div>
              <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>{desc}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
