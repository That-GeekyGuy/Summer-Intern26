import { useEffect } from "react";

interface ToastData { id: string; message: string; type: "info" | "success" | "error"; }

export function Toast({ toast, onDismiss }: { toast: ToastData; onDismiss: () => void }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 4000);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const color =
    toast.type === "error"   ? "var(--signal-critical)" :
    toast.type === "success" ? "var(--signal-ok)" :
    "var(--signal-info)";

  return (
    <div
      role="alert"
      style={{
        background: "var(--bg-elevated)",
        border: `1px solid ${color}`,
        borderLeft: `3px solid ${color}`,
        borderRadius: "var(--radius)",
        padding: "10px 14px",
        fontSize: "var(--text-sm)",
        color: "var(--text-primary)",
        maxWidth: 320,
        cursor: "pointer",
      }}
      onClick={onDismiss}
    >
      {toast.message}
    </div>
  );
}
