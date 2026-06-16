import { useState } from "react";
import { Credentials, saveCredentials } from "../api/client";
import { COPY } from "../lib/copy";

interface Props { onLogin: (c: Credentials) => void; }

export function LoginForm({ onLogin }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(""); setLoading(true);
    const creds: Credentials = { username: username.trim(), password };
    try {
      const res = await fetch("/api/v1/anomalies", {
        headers: { Authorization: "Basic " + btoa(`${creds.username}:${creds.password}`) },
      });
      if (!res.ok) throw new Error("Invalid credentials");
      saveCredentials(creds);
      onLogin(creds);
    } catch {
      setError("Invalid username or password");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      height: "100%", background: "var(--bg-base)",
    }}>
      <div style={{
        background: "var(--bg-surface)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: 40, width: 360,
      }}>
        <h1 style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-lg)", fontWeight: 700, marginBottom: 4 }}>
          {COPY.app.name}
        </h1>
        <p style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", marginBottom: 28 }}>
          {COPY.app.tagline}
        </p>
        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            Username
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              autoComplete="username"
              required
              style={{ background: "var(--bg-subtle)", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "8px 12px", color: "var(--text-primary)", fontSize: "var(--text-base)", fontFamily: "var(--font-body)", outline: "none" }}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            Password
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete="current-password"
              required
              style={{ background: "var(--bg-subtle)", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "8px 12px", color: "var(--text-primary)", fontSize: "var(--text-base)", fontFamily: "var(--font-body)", outline: "none" }}
            />
          </label>
          {error && <span style={{ color: "var(--signal-critical)", fontSize: "var(--text-xs)" }}>{error}</span>}
          <button
            type="submit"
            disabled={loading}
            style={{
              padding: "10px", background: "var(--accent)", color: "#fff", border: "none",
              borderRadius: "var(--radius)", fontSize: "var(--text-base)", fontFamily: "var(--font-body)",
              cursor: "pointer", fontWeight: 500, opacity: loading ? 0.7 : 1,
            }}
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
