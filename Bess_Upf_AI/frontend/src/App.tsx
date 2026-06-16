import { useState } from "react";
import { Credentials, getCredentials, clearCredentials } from "./api/client";
import { LoginForm } from "./components/LoginForm";
import { AppShell } from "./components/layout/AppShell";

export default function App() {
  const [creds, setCreds] = useState<Credentials | null>(() => getCredentials());

  function handleLogin(c: Credentials) { setCreds(c); }
  function handleLogout() { clearCredentials(); setCreds(null); }

  if (!creds) return <LoginForm onLogin={handleLogin} />;
  return <AppShell creds={creds} onLogout={handleLogout} />;
}
