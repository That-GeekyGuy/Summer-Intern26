import { Credentials } from "../../api/client";
import { COPY } from "../../lib/copy";
import { useAppStore } from "../../store/useAppStore";
import { PulseStrip } from "./PulseStrip";

interface Props {
  creds: Credentials;
  onLogout: () => void;
}

export function TopBar({ creds, onLogout }: Props) {
  const { theme, toggleTheme } = useAppStore();

  return (
    <header style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "16px 32px",
      flexShrink: 0,
      zIndex: 10,
    }}>
      {/* Brand */}
      <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
        <div style={{ display: "flex", flexDirection: "column" }}>
          <span style={{ fontWeight: 600, fontSize: "var(--text-sm)" }}>
            {COPY.app.name}
          </span>
          <span style={{ color: "var(--text-muted)", fontSize: 11, fontFamily: "var(--font-mono)" }}>
            {COPY.app.tagline}
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 12px", background: "var(--bg-elevated)", borderRadius: 100, fontSize: "var(--text-sm)" }}>
            <span style={{ color: "var(--text-secondary)" }}>User:</span>
            <span style={{ fontFamily: "var(--font-mono)", fontWeight: 500 }}>{creds.username}</span>
          </div>
        </div>
      </div>

      {/* Network Pulse */}
      <div style={{ flex: 1, display: "flex", justifyContent: "center" }}>
        <PulseStrip creds={creds} />
      </div>

      {/* Controls */}
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <button
          onClick={toggleTheme}
          style={{
            background: "var(--bg-surface)",
            border: "1px solid var(--border)",
            color: "var(--text-primary)",
            borderRadius: 100,
            padding: "8px 16px",
            fontSize: "var(--text-sm)",
            fontWeight: 600,
            cursor: "pointer",
            fontFamily: "var(--font-body)",
            transition: "all 0.2s ease",
            boxShadow: "var(--shadow-soft)",
          }}
          aria-label="Toggle theme"
        >
          {theme === "light" ? "🌙 Dark" : "☀️ Light"}
        </button>

        <button
          onClick={onLogout}
          style={{
            background: "transparent",
            border: "none",
            color: "var(--text-muted)",
            padding: "8px 16px",
            fontSize: "var(--text-sm)",
            fontWeight: 600,
            cursor: "pointer",
            fontFamily: "var(--font-body)",
            transition: "all 0.2s ease",
          }}
          aria-label="Log out"
          onMouseEnter={(e) => (e.currentTarget.style.color = "var(--text-primary)")}
          onMouseLeave={(e) => (e.currentTarget.style.color = "var(--text-muted)")}
        >
          Logout
        </button>
      </div>
    </header>
  );
}
