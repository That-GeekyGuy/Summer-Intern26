import React from "react";
import { Credentials, AnomalyEvent } from "../../api/client";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { displayName, formatLabels, severityColor } from "../../lib/metrics";
import { fmtTimestamp, fmtEta } from "../../lib/formatters";

interface Props { creds: Credentials; }

export function ContextPanel({ creds: _creds }: Props) {
  const { context, setPrefillChat, setActiveView } = useAppStore();

  function handleAskAI(question: string) {
    setPrefillChat(question);
    setActiveView("chat");
  }

  return (
    <aside style={{
      width: 320,
      flexShrink: 0,
      display: "flex",
      flexDirection: "column",
      background: "var(--bg-surface)",
      borderLeft: "1px solid var(--border)",
      overflow: "hidden",
    }}>
      {/* Panel header */}
      <div style={{
        padding: "10px 16px",
        borderBottom: "1px solid var(--border)",
        fontSize: "var(--text-xs)",
        color: "var(--text-muted)",
        fontFamily: "var(--font-mono)",
        textTransform: "uppercase",
        letterSpacing: "0.5px",
        flexShrink: 0,
      }}>
        {COPY.contextPanel.defaultTitle}
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: 16 }}>
        {context.type === "empty" && (
          <p style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", lineHeight: 1.6 }}>
            {COPY.contextPanel.defaultBody}
          </p>
        )}

        {context.type === "event" && context.event && (
          <EventDetail
            event={context.event}
            onAskAI={handleAskAI}
          />
        )}

        {context.type === "chat" && (
          <ChatContext
            queriesUsed={context.queriesUsed ?? []}
            anomalyCount={context.anomalyCount ?? 0}
          />
        )}
      </div>
    </aside>
  );
}

interface RCAReportData {
  severity?: string;
  cause?: string;
  summary?: string;
  evidence?: string[];
  recommended_actions?: string[];
  confidence?: number;
  eta_minutes?: number | null;
  model_attribution?: string;
}

function EventDetail({ event: e, onAskAI }: {
  event: AnomalyEvent;
  onAskAI: (q: string) => void;
}) {
  const isPredict = e.event_type === "predictive";
  const isML     = e.event_type === "ml";
  const isAI     = isML && !!e.rca_report;
  const color = severityColor(e.severity);
  const metricName = displayName(e.metric_name);
  const iface = formatLabels(e.labels);

  const question = isML
    ? `Explain this ${isAI ? "AI" : "ML"} anomaly detected by ${e.rule_name}: multivariate UPF behaviour deviated from trained normal patterns at ${fmtTimestamp(e.timestamp)}. Anomaly score: ${e.observed_value.toFixed(4)}.`
    : `Explain this ${isPredict ? "forecast" : "anomaly"}: ${metricName} on ${iface} — ${isPredict ? "projected to breach capacity" : `observed ${e.observed_value.toFixed(2)}, expected ~${(e.expected_value ?? 0).toFixed(2)}`}. Detected at ${fmtTimestamp(e.timestamp)}.`;

  let parsedLabels: Record<string, string> = {};
  try { parsedLabels = JSON.parse(e.labels); } catch { /* empty */ }

  // Classic ML: [{name, importance}] array
  let featureContribs: Array<{ name: string; importance: number }> = [];
  // AI: {channel: score} map — convert to sorted array for bar chart
  let channelScores: Array<{ name: string; importance: number }> = [];

  if (e.feature_contributions) {
    try {
      const parsed = JSON.parse(e.feature_contributions);
      if (Array.isArray(parsed)) {
        featureContribs = parsed;
      } else if (typeof parsed === "object" && parsed !== null) {
        channelScores = Object.entries(parsed as Record<string, number>)
          .map(([name, score]) => ({ name, importance: score }))
          .sort((a, b) => b.importance - a.importance)
          .slice(0, 5);
      }
    } catch { /* empty */ }
  }

  let rca: RCAReportData | null = null;
  if (e.rca_report) {
    try { rca = JSON.parse(e.rca_report) as RCAReportData; } catch { /* empty */ }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Event header */}
      <div style={{ borderLeft: `3px solid ${color}`, paddingLeft: 10 }}>
        <div style={{ fontSize: "var(--text-sm)", fontWeight: 600, marginBottom: 2 }}>
          {isAI ? "AI Anomaly (MOMENT)" : isML ? "Multivariate ML Anomaly" : metricName}
        </div>
        <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
          {isML ? "" : `${iface} · `}{e.rule_name}
        </div>
      </div>

      {/* Values */}
      <div style={{
        background: "var(--bg-subtle)",
        borderRadius: "var(--radius)",
        border: "1px solid var(--border)",
        padding: "10px 12px",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}>
        <Row label={isML ? "Anomaly score" : "Observed"} value={e.observed_value.toFixed(isML ? 4 : 2)} mono />
        {!isML && e.expected_value !== undefined && (
          <Row label="Expected" value={e.expected_value.toFixed(2)} mono />
        )}
        <Row label="Severity" value={e.severity.toUpperCase()} color={color} />
        <Row label="Time" value={fmtTimestamp(e.timestamp)} />
        {isPredict && e.predicted_crossing_time && (
          <Row label="ETA to breach" value={fmtEta(e.predicted_crossing_time)} color="var(--signal-warning)" />
        )}
        {isPredict && e.confidence !== undefined && (
          <Row label="Confidence (R²)" value={`${(e.confidence * 100).toFixed(0)}%`} />
        )}
        {isML && !isAI && e.confidence !== undefined && (
          <Row label="RF probability" value={`${(e.confidence * 100).toFixed(0)}%`} />
        )}
        {isAI && e.confidence !== undefined && (
          <Row label="Detection confidence" value={`${(e.confidence * 100).toFixed(0)}%`} />
        )}
        {rca?.eta_minutes != null && (
          <Row
            label="Breach ETA"
            value={`~${rca.eta_minutes.toFixed(1)} min`}
            color="var(--signal-warning)"
          />
        )}
      </div>

      {/* Structured RCA report (AI events only) */}
      {isAI && rca && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {rca.cause && (
            <div style={{
              background: "rgba(180,100,255,0.08)",
              border: "1px solid rgba(180,100,255,0.25)",
              borderRadius: "var(--radius)",
              padding: "8px 12px",
            }}>
              <div style={{ fontSize: "var(--text-2xs)", color: "rgba(180,100,255,0.7)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 4, fontFamily: "var(--font-mono)" }}>
                Root Cause
              </div>
              <div style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)", lineHeight: 1.5 }}>
                {rca.cause}
              </div>
            </div>
          )}

          {rca.summary && (
            <div>
              <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 4, fontFamily: "var(--font-mono)" }}>
                Summary
              </div>
              <p style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", lineHeight: 1.6, margin: 0 }}>
                {rca.summary}
              </p>
            </div>
          )}

          {rca.evidence && rca.evidence.length > 0 && (
            <div>
              <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 4, fontFamily: "var(--font-mono)" }}>
                Evidence
              </div>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 3 }}>
                {rca.evidence.map((item, i) => (
                  <li key={i} style={{ display: "flex", gap: 6, fontSize: "var(--text-xs)", color: "var(--text-secondary)", lineHeight: 1.5 }}>
                    <span style={{ color: "var(--signal-warning)", flexShrink: 0 }}>›</span>
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {rca.recommended_actions && rca.recommended_actions.length > 0 && (
            <div>
              <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 4, fontFamily: "var(--font-mono)" }}>
                Recommended Actions
              </div>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 3 }}>
                {rca.recommended_actions.map((action, i) => (
                  <li key={i} style={{ display: "flex", gap: 6, fontSize: "var(--text-xs)", color: "var(--text-secondary)", lineHeight: 1.5 }}>
                    <span style={{ color: "var(--signal-ok)", flexShrink: 0 }}>✓</span>
                    <span>{action}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {rca.model_attribution && (
            <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              {rca.model_attribution}
            </div>
          )}
        </div>
      )}

      {/* Channel scores bar chart (AI events) */}
      {isAI && channelScores.length > 0 && (
        <div>
          <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 6, fontFamily: "var(--font-mono)" }}>
            Top Anomalous Channels
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {channelScores.map((ch, i) => {
              const maxScore = channelScores[0]?.importance ?? 1;
              const barWidth = Math.max(4, Math.round((ch.importance / (maxScore + 1e-9)) * 120));
              return (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{
                    height: 6,
                    width: barWidth,
                    background: "rgba(180,100,255,0.6)",
                    borderRadius: 2,
                    flexShrink: 0,
                  }} />
                  <span style={{ fontSize: "var(--text-2xs)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
                    {ch.name}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Feature contributions (classic ML events only — array format) */}
      {isML && !isAI && featureContribs.length > 0 && (
        <div>
          <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 6, fontFamily: "var(--font-mono)" }}>
            Top features
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {featureContribs.map((f, i) => (
              <div key={i} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{
                  height: 6,
                  width: `${Math.round(f.importance * 200)}px`,
                  minWidth: 4,
                  maxWidth: 120,
                  background: "rgba(180,100,255,0.5)",
                  borderRadius: 2,
                  flexShrink: 0,
                }} />
                <span style={{ fontSize: "var(--text-2xs)", fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
                  {f.name}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Pending RCA placeholder for AI events awaiting async Brain 2 */}
      {isML && !isAI && e.feature_contributions && (
        <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", fontStyle: "italic" }}>
          RCA report pending Brain 2 analysis…
        </div>
      )}

      {/* Raw labels — omit for ML events (labels is always {}) */}
      {!isML && (
        <div>
          <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 4 }}>
            Raw Labels
          </div>
          <pre style={{
            fontFamily: "var(--font-mono)",
            fontSize: "var(--text-2xs)",
            color: "var(--text-secondary)",
            background: "var(--bg-subtle)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            padding: "6px 8px",
            overflowX: "auto",
            whiteSpace: "pre-wrap",
            wordBreak: "break-all",
          }}>
            {JSON.stringify(parsedLabels, null, 2)}
          </pre>
        </div>
      )}

      {/* Ask AI button */}
      <button
        onClick={() => onAskAI(question)}
        style={{
          padding: "8px 14px",
          background: "var(--accent-glow)",
          border: "1px solid var(--accent)",
          borderRadius: "var(--radius)",
          color: "var(--accent)",
          fontSize: "var(--text-sm)",
          fontFamily: "var(--font-body)",
          cursor: "pointer",
          fontWeight: 500,
        }}
      >
        {COPY.contextPanel.askAI}
      </button>
    </div>
  );
}

function ChatContext({ queriesUsed, anomalyCount }: { queriesUsed: string[]; anomalyCount: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Section title={COPY.contextPanel.queries}>
        {queriesUsed.length === 0 ? (
          <p style={{ fontSize: "var(--text-sm)", color: "var(--text-muted)" }}>
            {COPY.contextPanel.noQueries}
          </p>
        ) : (
          <ul style={{ listStyle: "none", display: "flex", flexDirection: "column", gap: 4 }}>
            {queriesUsed.map((q, i) => (
              <li key={i}>
                <code style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--text-2xs)",
                  background: "var(--bg-subtle)",
                  border: "1px solid var(--border)",
                  borderRadius: 3,
                  padding: "2px 6px",
                  display: "block",
                  wordBreak: "break-all",
                  color: "var(--text-secondary)",
                }}>
                  {q}
                </code>
              </li>
            ))}
          </ul>
        )}
      </Section>
      <Section title={COPY.contextPanel.events}>
        <p style={{ fontSize: "var(--text-sm)", color: "var(--text-muted)" }}>
          {anomalyCount} event{anomalyCount !== 1 ? "s" : ""} referenced
        </p>
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{
        fontSize: "var(--text-2xs)",
        color: "var(--text-muted)",
        textTransform: "uppercase",
        letterSpacing: "0.5px",
        marginBottom: 6,
        fontFamily: "var(--font-mono)",
      }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function Row({ label, value, mono, color }: { label: string; value: string; mono?: boolean; color?: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: "var(--text-xs)" }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span style={{
        fontFamily: mono ? "var(--font-mono)" : undefined,
        color: color ?? "var(--text-primary)",
        textAlign: "right",
      }}>
        {value}
      </span>
    </div>
  );
}
