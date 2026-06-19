import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Credentials } from "../../api/client";
import { useAppStore } from "../../store/useAppStore";
import { TopBar } from "./TopBar";
import { Sidebar } from "./Sidebar";
import { ContextPanel } from "./ContextPanel";
import { Toast } from "../primitives/Toast";
import { OnboardingOverlay } from "../primitives/OnboardingOverlay";
import { ENABLE_SCENARIO } from "../../lib/constants";

// Views
import { OverviewPage } from "../../features/overview/OverviewPage";
import { AnomalyFeedPage } from "../../features/anomalies/AnomalyFeedPage";
import { ChatPage } from "../../features/chat/ChatPage";
import { ForecastPage } from "../../features/forecast/ForecastPage";
import { InsightsPage } from "../../features/insights/InsightsPage";

const ScenarioPage = ENABLE_SCENARIO
  ? React.lazy(() => import("../../features/scenario/ScenarioPage").then(m => ({ default: m.ScenarioPage })))
  : null;

const qc = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

interface Props {
  creds: Credentials;
  onLogout: () => void;
}

export function AppShell({ creds, onLogout }: Props) {
  const { activeView, toasts, removeToast, onboardingDone } = useAppStore();

  function renderMain() {
    switch (activeView) {
      case "overview":  return <OverviewPage creds={creds} />;
      case "anomalies": return <AnomalyFeedPage creds={creds} />;
      case "chat":      return <ChatPage creds={creds} />;
      case "forecast":  return <ForecastPage creds={creds} />;
      case "insights":  return <InsightsPage creds={creds} />;
      case "scenario":
        if (!ENABLE_SCENARIO || !ScenarioPage) return (
          <div style={{ padding: 24, color: "var(--text-muted)" }}>
            Scenario controls are disabled in this build.
          </div>
        );
        return (
          <React.Suspense fallback={null}>
            <ScenarioPage creds={creds} />
          </React.Suspense>
        );
      default: return <OverviewPage creds={creds} />;
    }
  }

  return (
    <QueryClientProvider client={qc}>
      <div style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "var(--bg-base)",
        color: "var(--text-primary)",
        fontFamily: "var(--font-body)",
      }}>
        <TopBar creds={creds} onLogout={onLogout} />

        <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
          <Sidebar creds={creds} />

          {/* Main content */}
          <main style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
            {renderMain()}
          </main>

          <ContextPanel creds={creds} />
        </div>

        {/* Toast notifications */}
        <div style={{
          position: "fixed", bottom: 24, right: 24,
          display: "flex", flexDirection: "column", gap: 8, zIndex: 200,
        }}>
          {toasts.map(t => (
            <Toast key={t.id} toast={t} onDismiss={() => removeToast(t.id)} />
          ))}
        </div>

        {/* First-time onboarding */}
        {!onboardingDone && activeView === "overview" && <OnboardingOverlay />}
      </div>
    </QueryClientProvider>
  );
}
