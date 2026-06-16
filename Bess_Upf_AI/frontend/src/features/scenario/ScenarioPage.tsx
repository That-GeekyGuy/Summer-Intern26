import { useState } from "react";
import { Credentials, fetchScenario, setScenario, ScenarioStatus } from "../../api/client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";

interface Props { creds: Credentials; }

export function ScenarioPage({ creds }: Props) {
  const { addToast } = useAppStore();
  const qc = useQueryClient();
  const [duration, setDuration] = useState("10m");

  const { data: status } = useQuery<ScenarioStatus>({
    queryKey: ["scenario"],
    queryFn: () => fetchScenario(creds),
    refetchInterval: 5000,
  });

  const mutation = useMutation({
    mutationFn: ({ mode, dur }: { mode: string; dur?: string }) =>
      setScenario(creds, mode, dur),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ["scenario"] });
      const msg = vars.mode === "normal"
        ? COPY.scenario.toastStop
        : COPY.scenario.toastStart(vars.mode);
      addToast(msg, "info");
    },
  });

  const scenarios = Object.entries(COPY.scenario.descriptions);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      {/* Sim banner */}
      <div style={{
        background: "rgba(255,154,60,0.12)",
        borderBottom: "1px solid rgba(255,154,60,0.4)",
        padding: "8px 20px",
        fontSize: "var(--text-sm)",
        color: "var(--signal-warning)",
        fontFamily: "var(--font-mono)",
        fontWeight: 600,
        flexShrink: 0,
      }}>
        {COPY.scenario.simBanner}
      </div>

      <div style={{ padding: 24, overflowY: "auto", flex: 1 }}>
        <h2 style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-lg)", fontWeight: 700, marginBottom: 8 }}>
          {COPY.scenario.title}
        </h2>
        {status && (
          <div style={{ marginBottom: 16, fontSize: "var(--text-sm)", color: "var(--text-secondary)" }}>
            Active: <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>{status.mode}</span>
            {status.uptime_seconds > 0 && (
              <span style={{ marginLeft: 12, color: "var(--text-muted)" }}>
                {COPY.scenario.activeSince(status.uptime_seconds)}
              </span>
            )}
          </div>
        )}

        <div style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
          <label style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>Duration</label>
          <select
            value={duration}
            onChange={e => setDuration(e.target.value)}
            style={{
              background: "var(--bg-subtle)", border: "1px solid var(--border)",
              borderRadius: "var(--radius)", color: "var(--text-primary)",
              padding: "4px 10px", fontSize: "var(--text-sm)", fontFamily: "var(--font-body)",
            }}
          >
            {["2m", "5m", "10m", "30m"].map(d => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 12 }}>
          {scenarios.map(([id, desc]) => {
            const isActive = status?.mode === id;
            return (
              <button
                key={id}
                onClick={() => mutation.mutate({ mode: isActive ? "normal" : id, dur: duration })}
                style={{
                  textAlign: "left",
                  background: isActive ? "var(--bg-elevated)" : "var(--bg-surface)",
                  border: `1px solid ${isActive ? "var(--signal-warning)" : "var(--border)"}`,
                  borderRadius: "var(--radius)",
                  padding: "14px 16px",
                  cursor: "pointer",
                  display: "flex",
                  flexDirection: "column",
                  gap: 6,
                }}
              >
                <div style={{
                  fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)", fontWeight: 600,
                  color: isActive ? "var(--signal-warning)" : "var(--text-primary)",
                }}>
                  {id}
                  {isActive && (
                    <span style={{ marginLeft: 8, fontSize: "var(--text-2xs)", color: "var(--signal-ok)" }}>
                      RUNNING
                    </span>
                  )}
                </div>
                <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", lineHeight: 1.5 }}>
                  {desc}
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
