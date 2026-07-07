import { motion } from "framer-motion";
import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";
import { ENABLE_SCENARIO } from "../../lib/constants";

const NAV_ITEMS = [
  { id: "overview",  label: COPY.nav.overview,  icon: "◈" },
  { id: "anomalies", label: COPY.nav.anomalies, icon: "⚡" },
  { id: "chat",      label: COPY.nav.chat,      icon: "◎" },
  { id: "forecast",  label: COPY.nav.forecast,  icon: "⟁" },
  { id: "benchmark", label: "Benchmark",        icon: "◉" },
  { id: "insights",  label: "Insights",         icon: "✧" },
  ...(ENABLE_SCENARIO ? [{ id: "scenario", label: COPY.nav.scenario, icon: "◇" }] : []),
] as const;

type NavId = "overview" | "anomalies" | "chat" | "forecast" | "benchmark" | "scenario" | "insights";

export function FloatingDock() {
  const { activeView, setActiveView } = useAppStore();

  return (
    <div style={{
      position: "fixed",
      bottom: 24,
      left: "50%",
      transform: "translateX(-50%)",
      display: "flex",
      alignItems: "center",
      gap: 4,
      background: "var(--bg-surface)",
      padding: 8,
      borderRadius: 100,
      border: "1px solid var(--border)",
      boxShadow: "var(--shadow-elevated)",
      backdropFilter: "blur(20px)",
      WebkitBackdropFilter: "blur(20px)",
      zIndex: 100,
    }}>
      {NAV_ITEMS.map((item) => {
        const active = activeView === item.id;
        return (
          <button
            key={item.id}
            onClick={() => setActiveView(item.id as NavId)}
            style={{
              position: "relative",
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "10px 16px",
              borderRadius: 100,
              background: "transparent",
              border: "none",
              color: active ? "var(--bg-surface)" : "var(--text-secondary)",
              fontSize: "var(--text-sm)",
              fontWeight: active ? 600 : 500,
              cursor: "pointer",
              transition: "color 0.2s ease",
              outline: "none",
            }}
          >
            {active && (
              <motion.div
                layoutId="dock-indicator"
                style={{
                  position: "absolute",
                  inset: 0,
                  background: "var(--text-primary)",
                  borderRadius: 100,
                  zIndex: -1,
                }}
                transition={{ type: "spring", stiffness: 400, damping: 30 }}
              />
            )}
            <span style={{ 
              fontFamily: "var(--font-mono)", 
              fontSize: 14,
              color: active ? "var(--bg-surface)" : "var(--text-muted)",
              transition: "color 0.2s ease"
            }}>
              {item.icon}
            </span>
            <span style={{ 
              color: active ? "var(--bg-base)" : "var(--text-secondary)" 
            }}>
              {item.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}
