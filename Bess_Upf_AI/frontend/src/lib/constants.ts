// All polling intervals and env-driven config. Change intervals here only.
// Prometheus scrape interval is 15 s, so polling faster than that returns stale data
// from VM's cache — 5 s anomalies and 3 s pulse are still useful for responsiveness.

export const POLL_INTERVALS = {
  anomalies: parseInt(import.meta.env.VITE_POLL_ANOMALIES_MS ?? "2000"),   // 2 s — near real-time
  pulse:     parseInt(import.meta.env.VITE_POLL_PULSE_MS ?? "1000"),        // 1 s — sparkline feels live
  health:    parseInt(import.meta.env.VITE_POLL_HEALTH_MS ?? "5000"),      // 5 s
  forecast:  parseInt(import.meta.env.VITE_POLL_FORECAST_MS ?? "5000"),   // 5 s — matches scrape interval
};

export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// Read scenario controls from env (default to true for testing)
export const ENABLE_SCENARIO = true;

// Pulse strip: how many seconds of history to show (2 min rolling window)
export const PULSE_WINDOW_SEC = 120;
