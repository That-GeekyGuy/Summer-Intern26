// All polling intervals and env-driven config. Change intervals here only.
// upf-sim ticks every 1 s and ClickHouse's Kafka-engine tables flush on the same
// cadence (stream_flush_interval_ms=1000 in init.sql), so data is queryable
// within ~1 s of being produced — polling faster than that just re-fetches
// the same row rather than surfacing anything newer.

export const POLL_INTERVALS = {
  anomalies: parseInt(import.meta.env.VITE_POLL_ANOMALIES_MS ?? "2000"),   // 2 s — near real-time
  pulse:     parseInt(import.meta.env.VITE_POLL_PULSE_MS ?? "1000"),        // 1 s — matches source tick rate
  health:    parseInt(import.meta.env.VITE_POLL_HEALTH_MS ?? "5000"),      // 5 s
  forecast:  parseInt(import.meta.env.VITE_POLL_FORECAST_MS ?? "2000"),   // 2 s — matches flush interval
};

export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// Read scenario controls from env (default to true for testing)
export const ENABLE_SCENARIO = true;

// Pulse strip: how many seconds of history to show (2 min rolling window)
export const PULSE_WINDOW_SEC = 120;
