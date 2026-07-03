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
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", padding: 16, gap: 16 }}>
      {/* Sim banner */}
      <div style={{
        background: "rgba(245, 158, 11, 0.1)",
        border: "1px solid rgba(245, 158, 11, 0.3)",
        borderRadius: "var(--radius)",
        padding: "12px 20px",
        fontSize: "var(--text-sm)",
        color: "var(--signal-warning)",
        fontFamily: "var(--font-mono)",
        fontWeight: 600,
        flexShrink: 0,
        boxShadow: "var(--shadow-soft)",
      }}>
        {COPY.scenario.simBanner}
      </div>

      <div style={{
        padding: 32, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column",
        background: "var(--bg-surface)", borderRadius: "var(--radius)", border: "1px solid var(--border)",
        boxShadow: "var(--shadow-soft)", gap: 24
      }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <h2 style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xl)", fontWeight: 700, margin: 0, color: "var(--text-primary)" }}>
            {COPY.scenario.title}
          </h2>
          {status && (
            <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)" }}>
              Active: <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)", fontWeight: 600 }}>{status.mode}</span>
              {status.uptime_seconds > 0 && (
                <span style={{ marginLeft: 12, color: "var(--text-muted)", background: "var(--bg-elevated)", padding: "2px 8px", borderRadius: 100 }}>
                  {COPY.scenario.activeSince(status.uptime_seconds)}
                </span>
              )}
            </div>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "16px", background: "var(--bg-base)", borderRadius: 16, border: "1px solid var(--border)", width: "fit-content" }}>
          <label style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", fontWeight: 500 }}>Duration</label>
          <select
            value={duration}
            onChange={e => setDuration(e.target.value)}
            style={{
              background: "var(--bg-surface)", border: "1px solid var(--border)",
              borderRadius: 8, color: "var(--text-primary)",
              padding: "6px 12px", fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)",
              outline: "none", cursor: "pointer", boxShadow: "inset 0 2px 4px rgba(0,0,0,0.02)"
            }}
          >
            {["2m", "5m", "10m", "30m"].map(d => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 16 }}>
          {scenarios.map(([id, desc]) => {
            const isActive = status?.mode === id;
            return (
              <button
                key={id}
                onClick={() => mutation.mutate({ mode: isActive ? "normal" : id, dur: duration })}
                style={{
                  textAlign: "left",
                  background: isActive ? "rgba(245, 158, 11, 0.15)" : "var(--bg-base)",
                  border: `1px solid ${isActive ? "var(--signal-warning)" : "var(--border)"}`,
                  borderRadius: 24,
                  padding: "20px",
                  cursor: "pointer",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  boxShadow: isActive ? "0 0 0 1px var(--signal-warning)" : "var(--shadow-soft)",
                  transition: "all 0.2s",
                  position: "relative",
                  overflow: "hidden"
                }}
                onMouseEnter={e => {
                  if (!isActive) e.currentTarget.style.borderColor = "var(--border-focus)";
                  e.currentTarget.style.transform = "translateY(-1px)";
                }}
                onMouseLeave={e => {
                  if (!isActive) e.currentTarget.style.borderColor = "var(--border)";
                  e.currentTarget.style.transform = "none";
                }}
              >
                {isActive && <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 4, background: "var(--signal-warning)" }} />}
                <div style={{
                  fontFamily: "var(--font-mono)", fontSize: "var(--text-base)", fontWeight: 600,
                  color: isActive ? "var(--signal-warning)" : "var(--text-primary)",
                  display: "flex", alignItems: "center", justifyContent: "space-between",
                  width: "100%", paddingLeft: isActive ? 4 : 0
                }}>
                  {id}
                  {isActive && (
                    <span style={{ fontSize: "var(--text-2xs)", color: "var(--signal-warning)", background: "rgba(245, 158, 11, 0.1)", padding: "2px 8px", borderRadius: 100 }}>
                      RUNNING
                    </span>
                  )}
                </div>
                <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", lineHeight: 1.5, paddingLeft: isActive ? 4 : 0 }}>
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
