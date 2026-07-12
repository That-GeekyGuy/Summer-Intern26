// ---- Types ----------------------------------------------------------------

export interface FeatureContribution {
  name: string;
  importance: number;
}

export interface AnomalyEvent {
  metric_name: string;
  labels: string; // JSON string of label map
  timestamp: string;
  observed_value: number;
  expected_value?: number;
  deviation_magnitude: number;
  rule_name: string;
  severity: string;
  event_type?: "reactive" | "predictive" | "ml";
  // Predictive fields — only present when event_type === "predictive"
  forecast_horizon?: string;
  predicted_crossing_time?: number; // Unix seconds
  threshold_config?: string;        // JSON: {"capacity": N, "metric": "..."}
  // Shared: R² for predictive, RF probability for ml
  confidence?: number;
  // ML fields — only present when event_type === "ml"
  feature_contributions?: string;   // JSON: [{name, importance}×3] for classic ML, or {channel: score} map for AI
  rca_report?: string;              // JSON: RCAReport — populated async by Brain 2 after MOMENT detection
  created_at: string;
}

export interface ChatResponse {
  session_id: string;
  answer: string;
  queries_used: string[];
  anomaly_count: number;
  anomalies?: AnomalyEvent[];
}

export interface AnomalyListResponse {
  anomalies: AnomalyEvent[];
  total: number;
}

export interface ScenarioStatus {
  mode: string;
  started_at: string;
  duration?: string;
  uptime_seconds: number;
  remaining_seconds?: number;
}

export interface Credentials {
  username: string;
  password: string;
}

// ---- Credentials ----------------------------------------------------------

const CREDS_KEY = "upf_monitor_creds";

export function getCredentials(): Credentials | null {
  const raw = sessionStorage.getItem(CREDS_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Credentials;
  } catch {
    return null;
  }
}

export function saveCredentials(creds: Credentials): void {
  console.warn("Security Notice: Credentials are saved in sessionStorage in plaintext. This is a temporary prototype solution.");
  sessionStorage.setItem(CREDS_KEY, JSON.stringify(creds));
}

export function clearCredentials(): void {
  sessionStorage.removeItem(CREDS_KEY);
}

// ---- HTTP helper ----------------------------------------------------------

function authHeader(creds: Credentials): string {
  return "Basic " + btoa(creds.username + ":" + creds.password);
}

async function request<T>(
  path: string,
  creds: Credentials,
  options: RequestInit = {}
): Promise<T> {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: authHeader(creds),
      ...(options.headers as Record<string, string> | undefined),
    },
  });

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`HTTP ${res.status}: ${text}`);
  }

  return res.json() as Promise<T>;
}

// ---- API calls ------------------------------------------------------------

export async function sendChatMessage(
  creds: Credentials,
  message: string,
  sessionId?: string
): Promise<ChatResponse> {
  return request<ChatResponse>("/api/v1/chat", creds, {
    method: "POST",
    body: JSON.stringify({ message, session_id: sessionId }),
  });
}

export async function fetchAnomalies(
  creds: Credentials,
  since?: number,
  severity?: string,
  metric?: string
): Promise<AnomalyListResponse> {
  const params = new URLSearchParams();
  if (since !== undefined) params.set("since", String(since));
  if (severity) params.set("severity", severity);
  if (metric) params.set("metric", metric);
  const qs = params.toString() ? "?" + params.toString() : "";
  return request<AnomalyListResponse>(`/api/v1/anomalies${qs}`, creds);
}

export async function fetchScenario(creds: Credentials): Promise<ScenarioStatus> {
  return request<ScenarioStatus>("/api/v1/scenario", creds);
}

export async function setScenario(
  creds: Credentials,
  mode: string,
  duration?: string
): Promise<ScenarioStatus> {
  return request<ScenarioStatus>("/api/v1/scenario", creds, {
    method: "POST",
    body: JSON.stringify({ mode, duration: duration || undefined }),
  });
}

// ---- Temporal intelligence ------------------------------------------------

export async function submitFeedback(
  creds: Credentials,
  metricName: string,
  timestamp: string | number,
  isTruePositive: boolean
): Promise<void> {
  let ts = timestamp;
  if (typeof timestamp === "string") {
    ts = Math.floor(new Date(timestamp).getTime() / 1000);
  }
  const resp = await fetch("/api/v1/feedback", {
    method: "POST",
    headers: {
      "Authorization": "Basic " + btoa(`${creds.username}:${creds.password}`),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      metric_name: metricName,
      timestamp: ts,
      is_true_positive: isTruePositive,
    }),
  });
  if (!resp.ok) {
    throw new Error(`Failed to submit feedback: ${resp.status}`);
  }
}

export type Regime = "low" | "normal" | "peak" | "surge";

export interface HourlyStat {
  hour: number;
  regime: Regime;
  confidence: number;
  expected_sessions_mean: number;
  expected_sessions_p10: number;
  expected_sessions_p90: number;
  historical_anomaly_rate: number;
  label: string;
}

export interface HotzoneResponse {
  generated_at: string;
  data_coverage_days: number;
  hourly: HourlyStat[];
  peak_hours: number[];
  trough_hours: number[];
  next_peak_in_minutes?: number;
  next_trough_in_minutes?: number;
  warning?: string;
}

export interface CalendarContext {
  day_of_week: string;
  hour_of_day: number;
  is_weekend: boolean;
  is_holiday: boolean;
  holiday_name?: string;
  is_day_before_holiday: boolean;
  is_day_after_holiday: boolean;
  week_of_month: number;
}

export interface CurrentRegime {
  regime: Regime;
  percentile: number;
}

export interface TemporalAnalysis {
  generated_at: string;
  data_coverage_days: number;
  calendar: CalendarContext;
  current_regime: CurrentRegime;
  peak_hours: number[];
  trough_hours: number[];
  minutes_to_next_peak?: number;
  minutes_to_next_trough?: number;
  warning?: string;
}

export async function fetchTemporalAnalysis(creds: Credentials): Promise<TemporalAnalysis> {
  return request<TemporalAnalysis>("/api/v1/temporal/analysis", creds);
}

export async function fetchHotzone(creds: Credentials): Promise<HotzoneResponse> {
  return request<HotzoneResponse>("/api/v1/temporal/hotzone", creds);
}

// ---- PromQL-style instant query --------------------------------------------
// Proxied through the analysis service at GET /api/v1/query?q=<promql>,
// which evaluates it against ClickHouse (not an actual VictoriaMetrics/Prometheus).
// Does NOT go through the LLM — sub-10ms, zero token budget consumed.

export interface InstantSample {
  labels?: Record<string, string>;
  value?: number;
  Labels?: Record<string, string>;
  Value?: number;
}

export interface InstantQueryResponse {
  query: string;
  samples: InstantSample[];
}

export async function queryInstant(
  creds: Credentials,
  promql: string
): Promise<InstantQueryResponse> {
  const qs = new URLSearchParams({ q: promql }).toString();
  return request<InstantQueryResponse>(`/api/v1/query?${qs}`, creds);
}

export interface PulseResponse {
  sessions: number;
  n3rx: number;
  n6tx: number;
  drops: number;
}

export async function fetchPulse(creds: Credentials): Promise<PulseResponse> {
  return request<PulseResponse>("/api/v1/pulse", creds);
}

export interface HealthResponse {
  [service: string]: string;
}

export async function fetchHealth(creds: Credentials): Promise<HealthResponse> {
  return request<HealthResponse>("/api/v1/health", creds);
}

// ---- Ablation benchmark report --------------------------------------------

export interface TierResult {
  id: string;
  label: string;
  method?: string;
  note?: string;
  available: boolean;
  precision?: number;
  recall?: number;
  f1?: number;
  auc_roc?: number | null;
  latency_p50_ms?: number;
  tp?: number; fp?: number; fn?: number; tn?: number;
}

export interface BenchmarkReport {
  available: boolean;
  message?: string;
  generated_at?: string;
  dataset_hash?: string;
  tiers?: TierResult[];
  ensemble?: TierResult;
}

export async function fetchBenchmark(creds: Credentials): Promise<BenchmarkReport> {
  try {
    return await request<BenchmarkReport>("/api/v1/benchmark", creds);
  } catch (e: unknown) {
    if (e instanceof Error && e.message.startsWith("HTTP 404")) {
      return { available: false, message: "Run tools/train/ablation.py to generate the benchmark report." };
    }
    throw e;
  }
}

// ---- Chronos-2 uncertainty intervals --------------------------------------

export interface IntervalsResponse {
  available: boolean;
  channel: string;
  p10?: number[];
  p50?: number[];
  p90?: number[];
  horizon?: string;
  timestamps?: string[];
}

export async function fetchIntervals(
  creds: Credentials,
  promql: string,
  horizon = "short"
): Promise<IntervalsResponse> {
  const qs = new URLSearchParams({ q: promql, horizon }).toString();
  return request<IntervalsResponse>(`/api/v1/intervals?${qs}`, creds);
}
