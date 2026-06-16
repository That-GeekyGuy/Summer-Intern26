import { Credentials } from "../../api/client";
import { COPY } from "../../lib/copy";

interface Props {
  creds: Credentials;
  onLogout: () => void;
}

export function TopBar({ creds, onLogout }: Props) {
  return (
    <header style={{
      display: "flex",
      alignItems: "center",
      gap: 16,
      padding: "0 20px",
      height: 48,
      background: "var(--bg-surface)",
      borderBottom: "1px solid var(--border)",
      flexShrink: 0,
      zIndex: 10,
    }}>
      {/* Logo */}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{
          fontFamily: "var(--font-mono)",
          fontWeight: 700,
          fontSize: 14,
          color: "var(--text-primary)",
          letterSpacing: "-0.3px",
        }}>
          {COPY.app.name}
        </span>
        <span style={{
          fontSize: "var(--text-2xs)",
          color: "var(--text-muted)",
          fontFamily: "var(--font-mono)",
          padding: "1px 6px",
          border: "1px solid var(--border)",
          borderRadius: 3,
        }}>
          v1.0
        </span>
      </div>

      <div style={{ flex: 1 }} />

      {/* Auth */}
      <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
        {creds.username}
      </span>
      <button
        onClick={onLogout}
        style={{
          background: "none",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          color: "var(--text-secondary)",
          padding: "4px 12px",
          fontSize: "var(--text-sm)",
          cursor: "pointer",
          fontFamily: "var(--font-body)",
        }}
        aria-label="Log out"
      >
        Logout
      </button>
    </header>
  );
}
