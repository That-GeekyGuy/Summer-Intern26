import { create } from "zustand";
import { AnomalyEvent } from "../api/client";
import { ChatMessage } from "../hooks/useChat";

type ActiveView = "overview" | "anomalies" | "chat" | "forecast" | "insights" | "benchmark" | "scenario";

interface ContextPanelState {
  type: "empty" | "event" | "chat" | "forecast";
  event?: AnomalyEvent;
  chatMessages?: ChatMessage[];
  queriesUsed?: string[];
  anomalyCount?: number;
}

interface Toast {
  id: string;
  message: string;
  type: "info" | "success" | "error";
}

interface AppState {
  activeView: ActiveView;
  setActiveView: (v: ActiveView) => void;

  context: ContextPanelState;
  setContext: (c: ContextPanelState) => void;

  prefillChat: string;
  setPrefillChat: (msg: string) => void;

  toasts: Toast[];
  addToast: (message: string, type?: Toast["type"]) => void;
  removeToast: (id: string) => void;

  onboardingDone: boolean;
  dismissOnboarding: () => void;

  theme: "light" | "dark";
  toggleTheme: () => void;
}

// Helper to initialize theme side-effect
const getInitialTheme = (): "light" | "dark" => {
  const saved = localStorage.getItem("upf_theme") as "light" | "dark" | null;
  const initial = saved || "light";
  document.documentElement.classList.add(initial);
  return initial;
};

export const useAppStore = create<AppState>((set) => ({
  activeView: "overview",
  setActiveView: (v) => set({ activeView: v }),

  context: { type: "empty" },
  setContext: (c) => set({ context: c }),

  prefillChat: "",
  setPrefillChat: (msg) => set({ prefillChat: msg }),

  toasts: [],
  addToast: (message, type = "info") =>
    set((s) => ({
      toasts: [...s.toasts, { id: crypto.randomUUID(), message, type }],
    })),
  removeToast: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  onboardingDone: localStorage.getItem("upf_onboarded") === "1",
  dismissOnboarding: () => {
    localStorage.setItem("upf_onboarded", "1");
    set({ onboardingDone: true });
  },

  theme: getInitialTheme(),
  toggleTheme: () => set((state) => {
    const nextTheme = state.theme === "light" ? "dark" : "light";
    localStorage.setItem("upf_theme", nextTheme);
    document.documentElement.classList.remove(state.theme);
    document.documentElement.classList.add(nextTheme);
    return { theme: nextTheme };
  }),
}));
