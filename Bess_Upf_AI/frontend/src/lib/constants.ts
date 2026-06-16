// All polling intervals and env-driven config. Change intervals here only.

export const POLL_INTERVALS = {
  anomalies: parseInt(import.meta.env.VITE_POLL_ANOMALIES_MS ?? "15000"),
  pulse:     parseInt(import.meta.env.VITE_POLL_PULSE_MS ?? "5000"),
  health:    parseInt(import.meta.env.VITE_POLL_HEALTH_MS ?? "30000"),
  forecast:  parseInt(import.meta.env.VITE_POLL_FORECAST_MS ?? "60000"),
};

export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export const ENABLE_SCENARIO = import.meta.env.VITE_ENABLE_SCENARIO_CONTROLS === "true";

// Pulse strip: how many seconds of history to show
export const PULSE_WINDOW_SEC = 60;
