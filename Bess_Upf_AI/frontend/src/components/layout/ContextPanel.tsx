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

function EventDetail({ event: e, onAskAI }: {
  event: AnomalyEvent;
  onAskAI: (q: string) => void;
}) {
  const isPredict = e.event_type === "predictive";
  const color = severityColor(e.severity);
  const metricName = displayName(e.metric_name);
  const iface = formatLabels(e.labels);
  const question = `Explain this ${isPredict ? "forecast" : "anomaly"}: ${metricName} on ${iface} — ${isPredict ? "projected to breach capacity" : `observed ${e.observed_value.toFixed(2)}, expected ~${(e.expected_value ?? 0).toFixed(2)}`}. Detected at ${fmtTimestamp(e.timestamp)}.`;

  let parsedLabels: Record<string, string> = {};
  try { parsedLabels = JSON.parse(e.labels); } catch { /* empty */ }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Event header */}
      <div style={{ borderLeft: `3px solid ${color}`, paddingLeft: 10 }}>
        <div style={{ fontSize: "var(--text-sm)", fontWeight: 600, marginBottom: 2 }}>
          {metricName}
        </div>
        <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
          {iface} · {e.rule_name}
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
        <Row label="Observed" value={e.observed_value.toFixed(2)} mono />
        {e.expected_value !== undefined && (
          <Row label="Expected" value={e.expected_value.toFixed(2)} mono />
        )}
        <Row label="Severity" value={e.severity.toUpperCase()} color={color} />
        <Row label="Time" value={fmtTimestamp(e.timestamp)} />
        {e.event_type === "predictive" && e.predicted_crossing_time && (
          <Row label="ETA to breach" value={fmtEta(e.predicted_crossing_time)} color="var(--signal-warning)" />
        )}
        {e.confidence !== undefined && (
          <Row label="Confidence (R²)" value={`${(e.confidence * 100).toFixed(0)}%`} />
        )}
      </div>

      {/* Raw labels */}
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
