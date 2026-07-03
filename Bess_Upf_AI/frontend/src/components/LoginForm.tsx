import { useState, useEffect } from "react";
import { Credentials, saveCredentials } from "../api/client";
import { COPY } from "../lib/copy";
import { motion, useAnimation } from "framer-motion";

interface Props { onLogin: (c: Credentials) => void; }

export function LoginForm({ onLogin }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const controls = useAnimation();

  useEffect(() => {
    controls.start({
      backgroundPosition: ["0% 50%", "100% 50%", "0% 50%"],
      transition: { duration: 15, ease: "linear", repeat: Infinity }
    });
  }, [controls]);

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
    <motion.div 
      animate={controls}
      style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        height: "100%", 
        background: "linear-gradient(270deg, var(--bg-base), var(--bg-surface), var(--bg-base))",
        backgroundSize: "200% 200%",
        position: "relative",
        overflow: "hidden"
      }}
    >
      {/* Dynamic particles background */}
      {[...Array(12)].map((_, i) => (
        <motion.div
          key={i}
          animate={{
            y: ["0%", "-100%", "0%"],
            x: [Math.random() * 100 - 50, Math.random() * 100 - 50, Math.random() * 100 - 50],
            opacity: [0.1, 0.4, 0.1],
            scale: [1, 1.5, 1],
          }}
          transition={{
            duration: Math.random() * 10 + 10,
            repeat: Infinity,
            ease: "easeInOut",
            delay: Math.random() * 5,
          }}
          style={{
            position: "absolute",
            width: Math.random() * 100 + 50,
            height: Math.random() * 100 + 50,
            borderRadius: "50%",
            background: i % 2 === 0 ? "var(--accent-glow)" : "var(--signal-info)",
            filter: "blur(80px)",
            top: `${Math.random() * 100}%`,
            left: `${Math.random() * 100}%`,
            zIndex: 0
          }}
        />
      ))}

      <motion.div 
        initial={{ opacity: 0, y: 50, rotateX: 20 }}
        animate={{ opacity: 1, y: 0, rotateX: 0 }}
        transition={{ type: "spring", stiffness: 200, damping: 20 }}
        style={{
          background: "rgba(var(--bg-surface-rgb), 0.7)",
          border: "1px solid rgba(255, 255, 255, 0.1)",
          borderRadius: 40,
          boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.25)",
          padding: 56, 
          width: 420,
          zIndex: 1,
          backdropFilter: "blur(30px)",
          WebkitBackdropFilter: "blur(30px)",
          perspective: 1000
        }}
      >
        <motion.div 
          initial={{ scale: 0.8 }} animate={{ scale: 1 }} transition={{ delay: 0.2, type: "spring" }}
          style={{ textAlign: "center", marginBottom: 40 }}
        >
          <div style={{
            width: 64, height: 64, margin: "0 auto 24px",
            background: "linear-gradient(135deg, var(--accent), var(--signal-info))",
            borderRadius: 20,
            display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: "0 10px 20px -5px var(--accent-glow)"
          }}>
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
            </svg>
          </div>
          <h1 style={{ margin: "0 0 12px 0", fontSize: 28, fontWeight: 800, letterSpacing: "-0.05em" }}>{COPY.app.name}</h1>
          <p style={{ margin: 0, color: "var(--text-secondary)", fontSize: "var(--text-sm)", lineHeight: 1.5 }}>
            {COPY.app.tagline}
          </p>
        </motion.div>

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          <motion.label whileFocus={{ scale: 1.02 }} style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: "var(--text-sm)", color: "var(--text-secondary)", fontWeight: 600 }}>
            Username
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              autoComplete="username"
              required
              style={{ 
                background: "rgba(0, 0, 0, 0.05)", border: "1px solid var(--border)", 
                borderRadius: 16, padding: "16px 20px", 
                color: "var(--text-primary)", fontSize: "var(--text-base)", 
                fontFamily: "var(--font-body)", outline: "none",
                transition: "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)"
              }}
              onFocus={e => {
                e.target.style.borderColor = "var(--border-focus)";
                e.target.style.background = "var(--bg-base)";
                e.target.style.boxShadow = "0 0 0 4px var(--accent-glow)";
              }}
              onBlur={e => {
                e.target.style.borderColor = "var(--border)";
                e.target.style.background = "rgba(0, 0, 0, 0.05)";
                e.target.style.boxShadow = "none";
              }}
            />
          </motion.label>
          <motion.label whileFocus={{ scale: 1.02 }} style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: "var(--text-sm)", color: "var(--text-secondary)", fontWeight: 600 }}>
            Password
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete="current-password"
              required
              style={{ 
                background: "rgba(0, 0, 0, 0.05)", border: "1px solid var(--border)", 
                borderRadius: 16, padding: "16px 20px", 
                color: "var(--text-primary)", fontSize: "var(--text-base)", 
                fontFamily: "var(--font-body)", outline: "none",
                transition: "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)"
              }}
              onFocus={e => {
                e.target.style.borderColor = "var(--border-focus)";
                e.target.style.background = "var(--bg-base)";
                e.target.style.boxShadow = "0 0 0 4px var(--accent-glow)";
              }}
              onBlur={e => {
                e.target.style.borderColor = "var(--border)";
                e.target.style.background = "rgba(0, 0, 0, 0.05)";
                e.target.style.boxShadow = "none";
              }}
            />
          </motion.label>
          
          {error && (
            <motion.div 
              initial={{ opacity: 0, y: -10, scale: 0.9 }} animate={{ opacity: 1, y: 0, scale: 1 }}
              style={{ 
                background: "rgba(239, 68, 68, 0.1)", border: "1px solid rgba(239, 68, 68, 0.2)",
                color: "var(--signal-critical)", padding: "12px 16px", borderRadius: 12, fontSize: "var(--text-sm)",
                textAlign: "center"
              }}
            >
              {error}
            </motion.div>
          )}

          <motion.button
            type="submit"
            disabled={loading}
            whileHover={{ scale: 1.03, boxShadow: "0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04)" }}
            whileTap={{ scale: 0.97 }}
            style={{
              padding: "18px", 
              background: "linear-gradient(135deg, var(--text-primary), var(--text-secondary))",
              color: "var(--bg-base)",
              border: "none",
              borderRadius: 16,
              fontSize: "var(--text-base)", 
              fontFamily: "var(--font-body)",
              cursor: "pointer", 
              fontWeight: 700, 
              opacity: loading ? 0.7 : 1,
              marginTop: 16,
              boxShadow: "0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05)",
              transition: "opacity 0.2s"
            }}
          >
            {loading ? (
              <motion.span animate={{ opacity: [0.5, 1, 0.5] }} transition={{ repeat: Infinity, duration: 1.5 }}>
                Authenticating...
              </motion.span>
            ) : "Sign in to Dashboard"}
          </motion.button>
        </form>
      </motion.div>
    </motion.div>
  );
}
