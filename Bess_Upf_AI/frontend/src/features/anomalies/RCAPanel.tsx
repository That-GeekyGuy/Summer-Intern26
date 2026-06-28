import { AnomalyEvent } from "../../api/client";
import { severityColor } from "../../lib/metrics";

interface RCAReport {
  severity: string;
  cause: string;
  summary: string;
  evidence: string[];
  recommended_actions: string[];
  confidence: number;
  model_attribution: string;
}

interface Props {
  event: AnomalyEvent;
}

// ----- helpers ---------------------------------------------------------------

function parseRCA(raw?: string): RCAReport | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as RCAReport;
  } catch {
    return null;
  }
}

function parseChannelScores(raw?: string): Record<string, number> | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    // Could be array [{name, importance}] or object {channel: score}
    if (Array.isArray(parsed)) {
      const map: Record<string, number> = {};
      for (const item of parsed as { name: string; importance: number }[]) {
        if (item.name) map[item.name] = item.importance ?? 0;
      }
      return map;
    }
    if (typeof parsed === "object" && parsed !== null) {
      return parsed as Record<string, number>;
    }
    return null;
  } catch {
    return null;
  }
}

const AI_CHANNEL_LABELS: Record<string, string> = {
  port_bytes_N3_rx_rate:    "N3 Inbound Throughput",
  port_bytes_N6_tx_rate:    "N6 Outbound Throughput",
  port_pkts_N3_rx_rate:     "N3 Packet Rate",
  port_dropped_N3_rx_rate:  "N3 Drop Rate",
  port_dropped_N6_rx_rate:  "N6 Drop Rate",
  pfcp_sessions_total:      "Active Sessions",
  pfcp_session_setup_rate:  "Session Setup Rate",
  dl_forwarding_efficiency: "DL Forwarding Eff.",
  dl_throughput_efficiency: "DL Throughput Eff.",
  drop_rate_percentage:     "Drop Rate (%)",
  tsi_value:                "TSI Value",
  uoi_session_component:    "UOI Session",
  uoi_throughput_component: "UOI Throughput",
  go_goroutines:            "Go Goroutines",
  go_heap_alloc_bytes:      "Heap Alloc",
  gc_pressure_rate:         "GC Pressure",
};

function channelLabel(key: string): string {
  return AI_CHANNEL_LABELS[key] ?? key.replace(/_/g, " ");
}

// ----- tier badge --------------------------------------------------------

function tierInfo(eventType?: string, ruleName?: string): { label: string; color: string; bg: string } {
  if (eventType === "reactive" || (ruleName && ruleName.toLowerCase().includes("zscore"))) {
    return { label: "T1 · Z-score", color: "#94a3b8", bg: "rgba(148,163,184,0.12)" };
  }
  if (ruleName?.includes("random_forest") || ruleName?.includes("isolation_forest")) {
    const which = ruleName.includes("rf") ? "RF" : "IF";
    return { label: `T2a · ${which}`, color: "#60a5fa", bg: "rgba(96,165,250,0.12)" };
  }
  if (eventType === "ml") {
    return { label: "T2b · MOMENT", color: "#a78bfa", bg: "rgba(167,139,250,0.12)" };
  }
  if (eventType === "predictive") {
    return { label: "T3 · OLS Forecast", color: "#34d399", bg: "rgba(52,211,153,0.12)" };
  }
  return { label: ruleName ?? eventType ?? "Unknown", color: "var(--text-muted)", bg: "var(--bg-elevated)" };
}

// ----- sub-components --------------------------------------------------------

function ConfidenceMeter({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color =
    pct >= 70 ? "var(--signal-ok)" : pct >= 40 ? "var(--signal-warning)" : "var(--signal-neutral)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div
        style={{
          flex: 1,
          height: 4,
          background: "var(--border)",
          borderRadius: 2,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            background: color,
            borderRadius: 2,
            transition: "width 0.5s ease",
          }}
        />
      </div>
      <span style={{ fontSize: "var(--text-2xs)", color, fontFamily: "var(--font-mono)", minWidth: 30 }}>
        {pct}%
      </span>
    </div>
  );
}

function ChannelBar({ name, score, maxScore }: { name: string; score: number; maxScore: number }) {
  const pct = maxScore > 0 ? (score / maxScore) * 100 : 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
      <span
        style={{
          fontSize: "var(--text-2xs)",
          color: "var(--text-secondary)",
          fontFamily: "var(--font-mono)",
          width: 160,
          flexShrink: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {channelLabel(name)}
      </span>
      <div
        style={{
          flex: 1,
          height: 6,
          background: "var(--border)",
          borderRadius: 3,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            background: pct > 60 ? "var(--signal-critical)" : pct > 30 ? "var(--signal-warning)" : "var(--signal-info)",
            borderRadius: 3,
          }}
        />
      </div>
      <span
        style={{
          fontSize: "var(--text-2xs)",
          fontFamily: "var(--font-mono)",
          color: "var(--text-muted)",
          minWidth: 42,
          textAlign: "right",
        }}
      >
        {score.toExponential(1)}
      </span>
    </div>
  );
}

// ----- main component --------------------------------------------------------

export function RCAPanel({ event }: Props) {
  const rca = parseRCA(event.rca_report);
  const channelScores = parseChannelScores(event.feature_contributions);

  const isAI = event.event_type === "ml" && event.metric_name?.includes("_ai");
  const hasChannelScores = channelScores && Object.keys(channelScores).length > 0;

  // Sort channels by score descending and take top 8
  const sortedChannels = hasChannelScores
    ? Object.entries(channelScores)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 8)
    : [];
  const maxScore = sortedChannels[0]?.[1] ?? 1;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>

      {/* Detection tier badge */}
      {(() => {
        const { label, color, bg } = tierInfo(event.event_type, event.rule_name);
        return (
          <div style={{
            display: "inline-flex", alignItems: "center", gap: 6,
            padding: "3px 10px", borderRadius: 6,
            background: bg, border: `1px solid ${color}`,
            color, fontSize: "var(--text-2xs)", fontFamily: "var(--font-mono)", fontWeight: 700,
            alignSelf: "flex-start",
          }}>
            {label}
            {event.confidence != null && (
              <span style={{ fontWeight: 400, opacity: 0.75 }}>
                {(event.confidence * 100).toFixed(0)}% conf
              </span>
            )}
          </div>
        );
      })()}

      {/* AI badge + anomaly score */}
      {isAI && (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{
              padding: "2px 10px",
              borderRadius: 6,
              background: "rgba(124, 58, 237, 0.15)",
              border: "1px solid rgba(124, 58, 237, 0.5)",
              color: "#a78bfa",
              fontSize: "var(--text-2xs)",
              fontFamily: "var(--font-mono)",
              fontWeight: 700,
              letterSpacing: "0.06em",
            }}
          >
            MOMENT-1-large
          </span>
          <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>
            Reconstruction-based anomaly detection
          </span>
        </div>
      )}

      {/* Anomaly score */}
      <div>
        <div
          style={{
            fontSize: "var(--text-2xs)",
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
            marginBottom: 4,
          }}
        >
          Anomaly Score
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <span
            style={{
              fontSize: "var(--text-xl)",
              fontWeight: 700,
              fontFamily: "var(--font-mono)",
              color: severityColor(event.severity),
            }}
          >
            {event.observed_value.toFixed(4)}
          </span>
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            deviation {event.deviation_magnitude.toFixed(4)}
          </span>
        </div>
      </div>

      {/* Channel attribution bars */}
      {sortedChannels.length > 0 && (
        <div>
          <div
            style={{
              fontSize: "var(--text-2xs)",
              color: "var(--text-muted)",
              textTransform: "uppercase",
              letterSpacing: "0.05em",
              marginBottom: 8,
            }}
          >
            Channel Attribution (top {sortedChannels.length})
          </div>
          {sortedChannels.map(([ch, score]) => (
            <ChannelBar key={ch} name={ch} score={score} maxScore={maxScore} />
          ))}
        </div>
      )}

      {/* RCA Report */}
      {rca ? (
        <div
          style={{
            background: "var(--bg-surface)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: 14,
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          {/* Header */}
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span
              style={{
                padding: "2px 8px",
                borderRadius: 4,
                background: `${severityColor(rca.severity)}22`,
                border: `1px solid ${severityColor(rca.severity)}`,
                color: severityColor(rca.severity),
                fontSize: "var(--text-2xs)",
                fontFamily: "var(--font-mono)",
                fontWeight: 700,
                textTransform: "uppercase",
              }}
            >
              {rca.severity}
            </span>
            <span
              style={{
                fontSize: "var(--text-sm)",
                fontWeight: 600,
                color: "var(--text-primary)",
              }}
            >
              {rca.cause}
            </span>
          </div>

          {/* Summary */}
          <p
            style={{
              fontSize: "var(--text-xs)",
              color: "var(--text-secondary)",
              lineHeight: 1.6,
              margin: 0,
            }}
          >
            {rca.summary}
          </p>

          {/* Evidence */}
          {rca.evidence?.length > 0 && (
            <div>
              <div
                style={{
                  fontSize: "var(--text-2xs)",
                  color: "var(--text-muted)",
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  marginBottom: 6,
                }}
              >
                Evidence
              </div>
              <ul style={{ margin: 0, padding: "0 0 0 16px" }}>
                {rca.evidence.map((e, i) => (
                  <li
                    key={i}
                    style={{
                      fontSize: "var(--text-xs)",
                      color: "var(--text-secondary)",
                      fontFamily: "var(--font-mono)",
                      marginBottom: 3,
                    }}
                  >
                    {e}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Recommended actions */}
          {rca.recommended_actions?.length > 0 && (
            <div>
              <div
                style={{
                  fontSize: "var(--text-2xs)",
                  color: "var(--text-muted)",
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  marginBottom: 6,
                }}
              >
                Recommended Actions
              </div>
              <ol style={{ margin: 0, padding: "0 0 0 16px" }}>
                {rca.recommended_actions.map((a, i) => (
                  <li
                    key={i}
                    style={{
                      fontSize: "var(--text-xs)",
                      color: "var(--text-secondary)",
                      marginBottom: 4,
                    }}
                  >
                    {a}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {/* Confidence + model attribution */}
          <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
            <div
              style={{
                fontSize: "var(--text-2xs)",
                color: "var(--text-muted)",
                textTransform: "uppercase",
                letterSpacing: "0.05em",
              }}
            >
              RCA Confidence
            </div>
            <ConfidenceMeter value={rca.confidence} />
            <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontStyle: "italic" }}>
              {rca.model_attribution}
            </div>
          </div>
        </div>
      ) : (
        /* Pending RCA placeholder */
        event.event_type === "ml" ? (
          <div
            style={{
              background: "var(--bg-surface)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 14,
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginBottom: 4 }}>
              Root Cause Analysis
            </div>
            <div
              style={{
                fontSize: "var(--text-2xs)",
                color: "var(--text-muted)",
                fontFamily: "var(--font-mono)",
                fontStyle: "italic",
              }}
            >
              generating… (Brain 2 async RCA — refresh in a few seconds)
            </div>
            <div
              style={{
                width: 32,
                height: 32,
                border: "2px solid var(--border)",
                borderTopColor: "var(--signal-info)",
                borderRadius: "50%",
                animation: "spin 1s linear infinite",
                margin: "10px auto 0",
              }}
            />
          </div>
        ) : null
      )}
    </div>
  );
}
