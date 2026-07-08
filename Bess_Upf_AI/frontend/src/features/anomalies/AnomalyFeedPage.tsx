import { useState, useMemo } from "react";

import { Credentials, AnomalyEvent } from "../../api/client";
import { useAnomalies } from "../../hooks/useAnomalies";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { displayName, formatLabels, severityColor, fmtMetricVal } from "../../lib/metrics";
import { fmtTimestamp, fmtRelative, fmtEta } from "../../lib/formatters";
import { Badge } from "../../components/primitives/Badge";
import { motion, Variants } from "framer-motion";

const containerVariants: Variants = {
  hidden: { opacity: 0 },
  show: {
    opacity: 1,
    transition: { staggerChildren: 0.05 }
  }
};

const rowVariants: Variants = {
  hidden: { opacity: 0, x: -20 },
  show: { opacity: 1, x: 0, transition: { type: "spring", stiffness: 300, damping: 24 } }
};



interface Props { creds: Credentials; }

export function AnomalyFeedPage({ creds }: Props) {
  const { setContext } = useAppStore();
  const [severityFilter, setSeverityFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState<"metrics" | "reactive" | "predictive" | "ml" | "all">("metrics");



  const { data, isFetching, dataUpdatedAt, error } = useAnomalies(creds, {
    severity: severityFilter || undefined,
  });

  const events = useMemo(() => {
    let list = data?.anomalies ?? [];
    if (typeFilter === "metrics") list = list.filter(e => e.event_type === "reactive" || e.event_type === "predictive");
    else if (typeFilter !== "all") list = list.filter(e => e.event_type === typeFilter);
    return list;
  }, [data, typeFilter]);

  function handleRowClick(e: AnomalyEvent) {
    setContext({ type: "event", event: e });
  }

  return (
    <motion.div 
      variants={containerVariants}
      initial="hidden"
      animate="show"
      style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", padding: 16, gap: 16 }}
    >
      {/* Bento Container */}
      <div style={{
        display: "flex", flexDirection: "column", flex: 1, overflow: "hidden",
        background: "var(--bg-surface)",
        borderRadius: "var(--radius)",
        border: "1px solid var(--border)",
        boxShadow: "var(--shadow-soft)",
      }}>
        {/* Filters */}
        <div style={{
          display: "flex",
          gap: 12,
          padding: "16px 20px",
          borderBottom: "1px solid var(--border)",
          alignItems: "center",
          flexShrink: 0,
        }}>
          <span style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)", fontWeight: 600, marginRight: 8 }}>Filter Feed</span>
          {(["", "critical", "high", "medium", "low"] as const).map(v => (
            <button
              key={v}
              onClick={() => setSeverityFilter(v)}
              style={{
                padding: "4px 12px",
                fontSize: "var(--text-xs)",
                fontFamily: "var(--font-mono)",
                background: severityFilter === v ? "var(--bg-elevated)" : "none",
                border: "1px solid var(--border)",
                borderRadius: 100,
                color: v === "" ? "var(--text-secondary)" : severityColor(v),
                cursor: "pointer",
                transition: "all 0.2s",
              }}
            >
              {v || "All Severity"}
            </button>
          ))}
          <div style={{ width: 1, height: 20, background: "var(--border)", margin: "0 8px" }} />
          {(["metrics", "reactive", "predictive", "ml", "all"] as const).map(v => (
            <button
              key={v}
              onClick={() => setTypeFilter(v)}
              style={{
                padding: "4px 12px",
                fontSize: "var(--text-xs)",
                fontFamily: "var(--font-mono)",
                background: typeFilter === v ? "var(--bg-elevated)" : "none",
                border: "1px solid var(--border)",
                borderRadius: 100,
                color: "var(--text-secondary)",
                cursor: "pointer",
                transition: "all 0.2s",
              }}
            >
              {v === "metrics" ? "Default" : v === "reactive" ? "LIVE" : v === "predictive" ? "FORECAST" : v === "ml" ? "ML" : "All Types"}
            </button>
          ))}
          <div style={{ flex: 1 }} />
          {isFetching && (
            <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>refreshing…</span>
          )}
          {dataUpdatedAt > 0 && (
            <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginLeft: 16 }}>
              {COPY.anomalies.emptyHint}: {fmtRelative(new Date(dataUpdatedAt).toISOString())}
            </span>
          )}
        </div>

        {/* Table */}
        <div style={{ flex: 1, overflowY: "auto", padding: "0 8px" }}>
          {error ? (
            <div style={{ padding: 48, textAlign: "center" }}>
              <div style={{ color: "var(--signal-critical)", fontSize: "var(--text-base)", fontWeight: 600, marginBottom: 8 }}>
                Detection service unavailable
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)", color: "var(--text-muted)" }}>
                {String(error)}
              </div>
            </div>
          ) : events.length === 0 ? (
            <div style={{ padding: 48, textAlign: "center", color: "var(--text-muted)", fontSize: "var(--text-base)" }}>
              <div style={{ marginBottom: 12 }}>{COPY.anomalies.empty}</div>
              {dataUpdatedAt > 0 && (
                <div style={{ fontSize: "var(--text-sm)" }}>
                  Last checked: {fmtTimestamp(new Date(dataUpdatedAt).toISOString())}
                </div>
              )}
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "separate", borderSpacing: "0 8px" }}>
              <thead>
                <tr>
                  {["", "Type", "Metric", "Interface", "Value", "Baseline", "Time", "Status"].map(h => (
                    <th key={h} style={{
                      padding: "8px 16px",
                      textAlign: "left",
                      fontSize: "var(--text-xs)",
                      color: "var(--text-muted)",
                      fontFamily: "var(--font-mono)",
                      textTransform: "uppercase",
                      letterSpacing: "0.5px",
                      fontWeight: 600,
                      position: "sticky",
                      top: 0,
                      background: "var(--bg-surface)",
                      whiteSpace: "nowrap",
                      zIndex: 10,
                    }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <motion.tbody variants={containerVariants} initial="hidden" animate="show">
                {events.map((e, i) => (
                  <motion.tr
                    variants={rowVariants}
                    key={i}
                    onClick={() => handleRowClick(e)}
                    style={{
                      cursor: "pointer",
                      background: "var(--bg-base)",
                      boxShadow: "var(--shadow-soft)",
                    }}
                    whileHover={{ scale: 1.01, zIndex: 20 }}
                  >
                    {/* Severity dot */}
                    <td style={{ padding: "12px 16px", width: 8, borderRadius: "12px 0 0 12px" }}>
                      <div style={{ width: 10, height: 10, borderRadius: "50%", background: severityColor(e.severity), boxShadow: `0 0 8px ${severityColor(e.severity)}80` }} />
                    </td>
                    <td style={{ padding: "12px 16px" }}>
                      {e.event_type === "ml" ? (
                        <Badge variant="ml">{e.rca_report ? COPY.anomalies.aiBadge : COPY.anomalies.mlBadge}</Badge>
                      ) : e.event_type === "predictive" ? (
                        <Badge variant="forecast">{COPY.anomalies.forecastBadge}</Badge>
                      ) : (
                        <Badge variant="live">{COPY.anomalies.liveBadge}</Badge>
                      )}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: "var(--text-base)", fontWeight: 500, color: "var(--text-primary)", maxWidth: 200 }}>
                      {displayName(e.metric_name)}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: "var(--text-sm)", color: "var(--text-secondary)" }}>
                      {formatLabels(e.labels)}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>
                      {fmtMetricVal(e.metric_name, e.observed_value)}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: "var(--text-sm)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                      {e.expected_value !== undefined
                        ? (e.event_type === "predictive"
                            ? fmtMetricVal(e.metric_name, e.expected_value)
                            : e.expected_value.toFixed(2))
                        : "—"}
                    </td>
                    <td style={{ padding: "12px 16px", fontSize: "var(--text-sm)", color: "var(--text-muted)", fontFamily: "var(--font-mono)", whiteSpace: "nowrap" }}>
                      {e.event_type === "predictive" && e.predicted_crossing_time
                        ? <span style={{ color: "var(--signal-warning)" }}>⏱ {fmtEta(e.predicted_crossing_time)}</span>
                        : fmtRelative(e.timestamp)}
                    </td>

                    <td style={{ padding: "12px 16px", borderRadius: "0 12px 12px 0" }}>
                      <span style={{ fontSize: "var(--text-xs)", color: "var(--signal-ok)", fontFamily: "var(--font-mono)", background: "rgba(16, 185, 129, 0.1)", padding: "4px 8px", borderRadius: 100 }}>
                        ACTIVE
                      </span>
                    </td>
                  </motion.tr>
                ))}
              </motion.tbody>
            </table>
          )}
        </div>
      </div>
    </motion.div>
  );
}
