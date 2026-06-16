import { Credentials, fetchAnomalies } from "../../api/client";
import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { displayName, formatLabels } from "../../lib/metrics";
import { fmtEta } from "../../lib/formatters";
import { POLL_INTERVALS } from "../../lib/constants";

interface Props { creds: Credentials; }

export function ForecastPage({ creds }: Props) {
  const { setContext } = useAppStore();

  const { data, error, isFetching } = useQuery({
    queryKey: ["predictions"],
    queryFn: () => fetchAnomalies(creds),
    refetchInterval: POLL_INTERVALS.forecast,
    placeholderData: (p) => p,
    retry: 1,
  });

  const forecasts = (data?.anomalies ?? []).filter(e => e.event_type === "predictive");

  return (
    <div style={{ padding: 24, overflow: "auto", height: "100%" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 20 }}>
        <h2 style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-lg)", fontWeight: 700, margin: 0 }}>
          {COPY.forecast.title}
        </h2>
        {isFetching && (
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>refreshing…</span>
        )}
      </div>
      {error ? (
        <div style={{ padding: 24, background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
          <div style={{ color: "var(--signal-critical)", fontSize: "var(--text-sm)", marginBottom: 6 }}>
            Unable to load forecast data — detection service unavailable.
          </div>
          <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            {String(error)}
          </div>
        </div>
      ) : forecasts.length === 0 ? (
        <div style={{ padding: 24, background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: "var(--radius)" }}>
          <p style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", margin: 0 }}>{COPY.forecast.noData}</p>
          <p style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)", marginTop: 6 }}>
            Predictive events are generated when a metric trend exceeds the detection threshold. Try running a traffic scenario to generate data.
          </p>
        </div>
      ) : (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          {forecasts.map((e, i) => {
            const etaH = e.predicted_crossing_time
              ? (e.predicted_crossing_time * 1000 - Date.now()) / 3_600_000
              : -1;

            return (
              <div
                key={i}
                onClick={() => setContext({ type: "event", event: e })}
                style={{
                  background: "var(--bg-surface)",
                  border: "1px solid var(--border)",
                  borderLeft: "3px solid var(--signal-info)",
                  borderRadius: "var(--radius)",
                  padding: "16px 20px",
                  width: 320,
                  cursor: "pointer",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                }}
              >
                <div style={{ fontSize: "var(--text-base)", fontWeight: 600 }}>
                  {displayName(e.metric_name)}
                </div>
                <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
                  {formatLabels(e.labels)}
                </div>

                {/* Simple bar indicator */}
                <div style={{ background: "var(--bg-subtle)", borderRadius: 3, height: 4, overflow: "hidden" }}>
                  <div style={{
                    height: "100%",
                    width: e.expected_value && e.expected_value > 0
                      ? `${Math.min(100, (e.observed_value / e.expected_value) * 100).toFixed(0)}%`
                      : "50%",
                    background: "var(--signal-info)",
                    borderRadius: 3,
                  }} />
                </div>

                <div style={{ display: "flex", justifyContent: "space-between", fontSize: "var(--text-xs)" }}>
                  <span style={{ color: "var(--text-muted)" }}>
                    Now: <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>{e.observed_value.toFixed(0)}</span>
                  </span>
                  {e.expected_value !== undefined && (
                    <span style={{ color: "var(--text-muted)" }}>
                      Threshold: <span style={{ fontFamily: "var(--font-mono)", color: "var(--signal-critical)" }}>{e.expected_value.toFixed(0)}</span>
                    </span>
                  )}
                </div>

                {e.predicted_crossing_time && (
                  <div style={{
                    padding: "6px 10px",
                    background: "rgba(255,154,60,0.1)",
                    border: "1px solid rgba(255,154,60,0.3)",
                    borderRadius: 4,
                    fontSize: "var(--text-xs)",
                    color: "var(--signal-warning)",
                    fontFamily: "var(--font-mono)",
                  }}>
                    {COPY.forecast.etaLabel}: {fmtEta(e.predicted_crossing_time)}
                  </div>
                )}

                {/* Plain-English summary */}
                <p style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", lineHeight: 1.6 }}>
                  {COPY.forecast.summary(displayName(e.metric_name), 4, etaH > 0 ? etaH : 0)}
                </p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
