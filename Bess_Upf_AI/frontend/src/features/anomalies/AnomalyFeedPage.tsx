import { useState, useMemo } from "react";
import { Credentials, AnomalyEvent } from "../../api/client";
import { useAnomalies } from "../../hooks/useAnomalies";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { displayName, formatLabels, severityColor } from "../../lib/metrics";
import { fmtTimestamp, fmtRelative, fmtEta } from "../../lib/formatters";
import { Badge } from "../../components/primitives/Badge";

interface Props { creds: Credentials; }

export function AnomalyFeedPage({ creds }: Props) {
  const { setContext } = useAppStore();
  const [severityFilter, setSeverityFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState<"" | "reactive" | "predictive">("");

  const { data, isFetching, dataUpdatedAt, error } = useAnomalies(creds, {
    severity: severityFilter || undefined,
  });

  const events = useMemo(() => {
    let list = data?.anomalies ?? [];
    if (typeFilter) list = list.filter(e => e.event_type === typeFilter);
    return list;
  }, [data, typeFilter]);

  function handleRowClick(e: AnomalyEvent) {
    setContext({ type: "event", event: e });
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      {/* Filters */}
      <div style={{
        display: "flex",
        gap: 8,
        padding: "10px 16px",
        borderBottom: "1px solid var(--border)",
        alignItems: "center",
        flexShrink: 0,
      }}>
        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginRight: 4 }}>Filter:</span>
        {(["", "critical", "high", "medium", "low"] as const).map(v => (
          <button
            key={v}
            onClick={() => setSeverityFilter(v)}
            style={{
              padding: "3px 10px",
              fontSize: "var(--text-xs)",
              fontFamily: "var(--font-mono)",
              background: severityFilter === v ? "var(--bg-elevated)" : "none",
              border: "1px solid var(--border)",
              borderRadius: 3,
              color: v === "" ? "var(--text-secondary)" : severityColor(v),
              cursor: "pointer",
            }}
          >
            {v || "All"}
          </button>
        ))}
        <div style={{ width: 1, height: 16, background: "var(--border)", margin: "0 4px" }} />
        {(["", "reactive", "predictive"] as const).map(v => (
          <button
            key={v}
            onClick={() => setTypeFilter(v)}
            style={{
              padding: "3px 10px",
              fontSize: "var(--text-xs)",
              fontFamily: "var(--font-mono)",
              background: typeFilter === v ? "var(--bg-elevated)" : "none",
              border: "1px solid var(--border)",
              borderRadius: 3,
              color: "var(--text-secondary)",
              cursor: "pointer",
            }}
          >
            {v === "" ? "All types" : v === "reactive" ? "LIVE" : "FORECAST"}
          </button>
        ))}
        <div style={{ flex: 1 }} />
        {isFetching && (
          <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>refreshing…</span>
        )}
        {dataUpdatedAt > 0 && (
          <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>
            {COPY.anomalies.emptyHint}: {fmtRelative(new Date(dataUpdatedAt).toISOString())}
          </span>
        )}
      </div>

      {/* Table */}
      <div style={{ flex: 1, overflowY: "auto" }}>
        {error ? (
          <div style={{ padding: 32, textAlign: "center" }}>
            <div style={{ color: "var(--signal-critical)", fontSize: "var(--text-sm)", marginBottom: 8 }}>
              Detection service unavailable
            </div>
            <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
              {String(error)}
            </div>
          </div>
        ) : events.length === 0 ? (
          <div style={{ padding: 32, textAlign: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
            <div style={{ marginBottom: 8 }}>{COPY.anomalies.empty}</div>
            {dataUpdatedAt > 0 && (
              <div style={{ fontSize: "var(--text-xs)" }}>
                Last checked: {fmtTimestamp(new Date(dataUpdatedAt).toISOString())}
              </div>
            )}
          </div>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border)" }}>
                {["", "Type", "Metric", "Interface", "Value", "Baseline", "Time", "Status"].map(h => (
                  <th key={h} style={{
                    padding: "8px 12px",
                    textAlign: "left",
                    fontSize: "var(--text-2xs)",
                    color: "var(--text-muted)",
                    fontFamily: "var(--font-mono)",
                    textTransform: "uppercase",
                    letterSpacing: "0.5px",
                    fontWeight: 600,
                    position: "sticky",
                    top: 0,
                    background: "var(--bg-base)",
                    whiteSpace: "nowrap",
                  }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {events.map((e, i) => (
                <tr
                  key={i}
                  onClick={() => handleRowClick(e)}
                  className="row-fade-in"
                  style={{
                    cursor: "pointer",
                    borderBottom: "1px solid var(--border)",
                    height: 36,
                  }}
                  onMouseEnter={ev => (ev.currentTarget.style.background = "var(--bg-surface)")}
                  onMouseLeave={ev => (ev.currentTarget.style.background = "")}
                >
                  {/* Severity stripe on the td — border-left on tr is unreliable with border-collapse */}
                  <td style={{ padding: "6px 12px", width: 8, borderLeft: `3px solid ${severityColor(e.severity)}` }}>
                    <div style={{ width: 8, height: 8, borderRadius: "50%", background: severityColor(e.severity) }} />
                  </td>
                  <td style={{ padding: "6px 12px" }}>
                    <Badge variant={e.event_type === "predictive" ? "forecast" : "live"}>
                      {e.event_type === "predictive" ? COPY.anomalies.forecastBadge : COPY.anomalies.liveBadge}
                    </Badge>
                  </td>
                  <td style={{ padding: "6px 12px", fontSize: "var(--text-sm)", maxWidth: 200 }}>
                    {displayName(e.metric_name)}
                  </td>
                  <td style={{ padding: "6px 12px", fontSize: "var(--text-sm)", color: "var(--text-secondary)" }}>
                    {formatLabels(e.labels)}
                  </td>
                  <td style={{ padding: "6px 12px", fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)" }}>
                    {e.observed_value.toFixed(2)}
                  </td>
                  <td style={{ padding: "6px 12px", fontSize: "var(--text-sm)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                    {e.expected_value !== undefined ? e.expected_value.toFixed(2) : "—"}
                  </td>
                  <td style={{ padding: "6px 12px", fontSize: "var(--text-xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)", whiteSpace: "nowrap" }}>
                    {e.event_type === "predictive" && e.predicted_crossing_time
                      ? `⏱ ${fmtEta(e.predicted_crossing_time)}`
                      : fmtRelative(e.timestamp)}
                  </td>
                  <td style={{ padding: "6px 12px" }}>
                    <span style={{ fontSize: "var(--text-xs)", color: "var(--signal-ok)", fontFamily: "var(--font-mono)" }}>
                      ACTIVE
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
