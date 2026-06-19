import React from "react";

interface Props {
  variant: "live" | "forecast" | "ml" | "severity";
  severity?: string;
  children: React.ReactNode;
}

export function Badge({ variant, severity, children }: Props) {
  const styles: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    fontSize: "var(--text-2xs)",
    fontWeight: 700,
    padding: "1px 6px",
    borderRadius: 3,
    letterSpacing: "0.3px",
    fontFamily: "var(--font-mono)",
    whiteSpace: "nowrap",
  };

  if (variant === "live") {
    return (
      <span style={{ ...styles, background: "rgba(255,77,77,0.15)", color: "var(--signal-critical)", border: "1px solid rgba(255,77,77,0.3)" }}>
        {children}
      </span>
    );
  }
  if (variant === "forecast") {
    return (
      <span style={{ ...styles, background: "rgba(61,127,232,0.15)", color: "var(--signal-info)", border: "1px solid rgba(61,127,232,0.3)" }}>
        {children}
      </span>
    );
  }
  if (variant === "ml") {
    return (
      <span style={{ ...styles, background: "rgba(180,100,255,0.15)", color: "var(--signal-warning)", border: "1px solid rgba(180,100,255,0.4)" }}>
        {children}
      </span>
    );
  }
  // severity badge
  const color =
    severity === "critical" ? "var(--signal-critical)" :
    severity === "high" || severity === "warning" ? "var(--signal-warning)" :
    "var(--signal-neutral)";
  return (
    <span style={{ ...styles, background: "transparent", color, border: `1px solid ${color}` }}>
      {children}
    </span>
  );
}
