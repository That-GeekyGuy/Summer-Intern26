import { Credentials } from "../../api/client";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { ENABLE_SCENARIO } from "../../lib/constants";
import { PulseStrip } from "./PulseStrip";

const NAV_ITEMS = [
  { id: "overview",  label: COPY.nav.overview,  icon: "◈" },
  { id: "anomalies", label: COPY.nav.anomalies,  icon: "⚡" },
  { id: "chat",      label: COPY.nav.chat,       icon: "◎" },
  { id: "forecast",  label: COPY.nav.forecast,   icon: "⟁" },
  { id: "insights",  label: "Insights",           icon: "◑" },
  ...(ENABLE_SCENARIO ? [{ id: "scenario", label: COPY.nav.scenario, icon: "◇" }] : []),
] as const;

type NavId = "overview" | "anomalies" | "chat" | "forecast" | "insights" | "scenario";

interface Props { creds: Credentials; }

export function Sidebar({ creds }: Props) {
  const { activeView, setActiveView } = useAppStore();

  return (
    <aside style={{
      width: 200,
      flexShrink: 0,
      display: "flex",
      flexDirection: "column",
      background: "var(--bg-surface)",
      borderRight: "1px solid var(--border)",
      overflow: "hidden",
    }}>
      {/* Pulse strip — always at top */}
      <PulseStrip creds={creds} />

      {/* Navigation */}
      <nav style={{ flex: 1, padding: "8px 0" }} aria-label="Main navigation">
        {NAV_ITEMS.map((item) => {
          const active = activeView === item.id;
          return (
            <button
              key={item.id}
              onClick={() => setActiveView(item.id as NavId)}
              aria-current={active ? "page" : undefined}
              style={{
                width: "100%",
                textAlign: "left",
                padding: "9px 16px",
                background: active ? "var(--bg-elevated)" : "none",
                border: "none",
                borderLeft: active ? "3px solid var(--accent)" : "3px solid transparent",
                color: active ? "var(--text-primary)" : "var(--text-secondary)",
                fontSize: "var(--text-sm)",
                fontFamily: "var(--font-body)",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                gap: 10,
                transition: "all 0.12s",
              }}
            >
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, opacity: 0.7 }}>{item.icon}</span>
              {item.label}
            </button>
          );
        })}
      </nav>

      {/* Bottom version strip */}
      <div style={{
        padding: "8px 16px",
        borderTop: "1px solid var(--border)",
        fontSize: "var(--text-2xs)",
        color: "var(--text-muted)",
        fontFamily: "var(--font-mono)",
      }}>
        BESS-UPF · C-DOT
      </div>
    </aside>
  );
}
