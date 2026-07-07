import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Credentials, fetchTemporalAnalysis, fetchHotzone, fetchAnomalies, Regime, HourlyStat } from "../../api/client";

interface Props { creds: Credentials; }

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

function HeatMapBar({ stat }: { stat: HourlyStat }) {
  const color = REGIME_COLOR[stat.regime] ?? "#888";
  const bg    = REGIME_BG[stat.regime] ?? "rgba(136,136,136,0.1)";
  return (
    <div title={`${fmtHour(stat.hour)}: ${regimeLabel(stat.regime)} (conf ${(stat.confidence * 100).toFixed(0)}%)`}
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

function CalendarCard({ analysis }: { analysis: ReturnType<typeof fetchTemporalAnalysis> extends Promise<infer T> ? T : never }) {
  const cal = analysis.calendar;
  const regime = analysis.current_regime;
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
        <Row label="Day"        value={`${cal.day_of_week}, ${fmtHour(cal.hour_of_day)} UTC`} />
        <Row label="Type"       value={cal.is_holiday ? `Holiday: ${cal.holiday_name ?? ""}` : cal.is_weekend ? "Weekend" : "Weekday"} />
        {cal.is_day_before_holiday && <Row label="Note" value="Day before holiday" />}
        {cal.is_day_after_holiday  && <Row label="Note" value="Day after holiday" />}
        <Row label="Week"       value={`Week ${cal.week_of_month} of month`} />
      </div>

      <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)" }}>
          Current Regime
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{
            padding: "3px 10px",
            borderRadius: 12,
            background: REGIME_BG[regime.regime] ?? "var(--bg-elevated)",
            border: `1px solid ${REGIME_COLOR[regime.regime] ?? "var(--border)"}`,
            color: REGIME_COLOR[regime.regime] ?? "var(--text-primary)",
            fontSize: "var(--text-xs)",
            fontFamily: "var(--font-mono)",
            fontWeight: 700,
            textTransform: "uppercase",
          }}>
            {regimeLabel(regime.regime)}
          </span>
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            {(regime.percentile * 100).toFixed(0)}th pct vs seasonal norm
          </span>
        </div>
      </div>

      {(analysis.minutes_to_next_peak != null || analysis.minutes_to_next_trough != null) && (
        <div style={{ display: "flex", gap: 16, fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
          {analysis.minutes_to_next_peak != null && (
            <span>Next peak in <strong>{analysis.minutes_to_next_peak} min</strong></span>
          )}
          {analysis.minutes_to_next_trough != null && (
            <span>Next trough in <strong>{analysis.minutes_to_next_trough} min</strong></span>
          )}
        </div>
      )}

      {analysis.warning && (
        <div style={{
          background: "rgba(230,126,34,0.1)", border: "1px solid rgba(230,126,34,0.3)",
          borderRadius: 4, padding: "6px 10px", fontSize: "var(--text-xs)", color: "#e67e22",
        }}>
          ⚠ {analysis.warning}
        </div>
      )}
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
  // v in [0,1]: 0=cool, 1=hot (co-activation strength)
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

function DataCoverageWarning({ days }: { days: number }) {
  if (days >= 3) return null;
  return (
    <div style={{
      background: "rgba(231,76,60,0.08)", border: "1px solid rgba(231,76,60,0.3)",
      borderRadius: 4, padding: "8px 14px", fontSize: "var(--text-xs)", color: "#e74c3c",
      marginBottom: 12,
    }}>
      ⚠ <strong>Thin baseline ({days.toFixed(1)} days).</strong> Each hour appears only {days < 1.5 ? "once" : "1–2 times"} in training data.
      Regime estimates are noisy until ≥ 7 days of data accumulates.
      {days < 3 && " Confidence capped at 30%."}
    </div>
  );
}

export function InsightsPage({ creds }: Props) {
  const { data: analysis, isLoading: aLoading, error: aError } = useQuery({
    queryKey: ["temporal-analysis"],
    queryFn: () => fetchTemporalAnalysis(creds),
    refetchInterval: 60_000,
    retry: 1,
  });

  const { data: hotzone, isLoading: hLoading, error: hError } = useQuery({
    queryKey: ["temporal-hotzone"],
    queryFn: () => fetchHotzone(creds),
    refetchInterval: 30 * 60_000,  // refetch every 30 min (server caches 1h)
    retry: 1,
  });

  const sidecarDown = !!(aError || hError);

  // Fetch recent ML anomaly events to derive channel co-activation matrix
  const { data: anomalyData } = useQuery({
    queryKey: ["anomalies-insights"],
    queryFn: () => fetchAnomalies(creds),
    staleTime: 60_000,
    retry: 1,
  });

  const { channels, matrix } = useMemo(() => {
    const mlEvents = (anomalyData?.anomalies ?? []).filter(e => e.event_type === "ml" && e.feature_contributions);
    if (mlEvents.length < 2) return { channels: [], matrix: [] as number[][] };

    // Collect all channel names
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
      } catch { /* skip malformed */ }
    }

    const chs = Array.from(chSet).slice(0, 10); // cap at 10 for readability
    if (chs.length < 2) return { channels: [], matrix: [] as number[][] };

    // Build channel score vectors (0 if channel absent in event)
    const vecs = vectors.map(sc => chs.map(ch => sc[ch] ?? 0));

    // Compute co-activation matrix: normalised dot-product between channel activation vectors
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
    // Normalise to [0,1] by max off-diagonal
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
          STL decomposition · regime detection · calendar awareness
        </span>
      </div>

      {sidecarDown && (
        <div style={{
          background: "var(--bg-surface)", border: "1px solid var(--border)",
          borderRadius: 6, padding: "20px 24px", textAlign: "center",
          color: "var(--text-muted)", fontSize: "var(--text-sm)",
        }}>
          <div style={{ fontSize: "1.5rem", marginBottom: 8 }}>◑</div>
          <div>STL sidecar is offline or STL_URL is not configured.</div>
          <div style={{ marginTop: 4, fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
            Set STL_URL=http://stl-sidecar:8085 in .env and restart the analysis service.
          </div>
        </div>
      )}

      {!sidecarDown && (
        <>
          {/* Data coverage warning (shown when < 3 days) */}
          {hotzone && <DataCoverageWarning days={hotzone.data_coverage_days} />}

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
                24-Hour Traffic Regime Forecast
              </div>

              {hLoading && (
                <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>Loading…</div>
              )}

              {hotzone && (
                <>
                  {/* Regime bar strip */}
                  <div style={{ display: "flex", gap: 2, alignItems: "stretch", height: 48 }}>
                    {hotzone.hourly.map(stat => (
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

                  {/* Peak / trough summary */}
                  <div style={{ display: "flex", gap: 20, fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
                    {hotzone.peak_hours.length > 0 && (
                      <span>Peak hours: <strong style={{ fontFamily: "var(--font-mono)" }}>{hotzone.peak_hours.map(fmtHour).join(", ")}</strong></span>
                    )}
                    {hotzone.trough_hours.length > 0 && (
                      <span>Trough hours: <strong style={{ fontFamily: "var(--font-mono)" }}>{hotzone.trough_hours.map(fmtHour).join(", ")}</strong></span>
                    )}
                  </div>

                  {hotzone.warning && (
                    <div style={{ fontSize: "var(--text-2xs)", color: "#e67e22", fontFamily: "var(--font-mono)" }}>
                      ⚠ {hotzone.warning}
                    </div>
                  )}
                </>
              )}
            </div>

            {/* Calendar card */}
            <div style={{ flex: "0 0 260px" }}>
              {aLoading && (
                <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)", padding: 12 }}>Loading…</div>
              )}
              {analysis && <CalendarCard analysis={analysis} />}
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
        </>
      )}
    </div>
  );
}
