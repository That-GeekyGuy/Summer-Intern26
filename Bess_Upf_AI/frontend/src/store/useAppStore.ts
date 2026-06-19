import { create } from "zustand";
import { AnomalyEvent } from "../api/client";
import { ChatMessage } from "../hooks/useChat";

type ActiveView = "overview" | "anomalies" | "chat" | "forecast" | "insights" | "scenario";

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
}

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
}));
