import { useAppStore } from "../../store/useAppStore";
import { COPY } from "../../lib/copy";

export function OnboardingOverlay() {
  const { dismissOnboarding } = useAppStore();

  return (
    <div style={{
      position: "fixed", inset: 0,
      background: "rgba(10,14,20,0.7)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 300,
    }}>
      <div style={{
        background: "var(--bg-surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: 28,
        width: 400,
        maxWidth: "90vw",
        display: "flex",
        flexDirection: "column",
        gap: 16,
      }}>
        <h2 style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-lg)", fontWeight: 700 }}>
          {COPY.onboarding.title}
        </h2>
        <p style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", lineHeight: 1.65 }}>
          {COPY.onboarding.body}
        </p>
        <button
          onClick={dismissOnboarding}
          autoFocus
          style={{
            alignSelf: "flex-end",
            padding: "8px 20px",
            background: "var(--accent)",
            border: "none",
            borderRadius: "var(--radius)",
            color: "#fff",
            fontSize: "var(--text-sm)",
            fontFamily: "var(--font-body)",
            cursor: "pointer",
            fontWeight: 500,
          }}
        >
          {COPY.onboarding.cta}
        </button>
      </div>
    </div>
  );
}
