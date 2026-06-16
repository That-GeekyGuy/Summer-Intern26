// ---- Types ----------------------------------------------------------------

export interface AnomalyEvent {
  metric_name: string;
  labels: string; // JSON string of label map
  timestamp: string;
  observed_value: number;
  expected_value?: number;
  deviation_magnitude: number;
  rule_name: string;
  severity: string;
  // Predictive forecast fields — only present when event_type === "predictive"
  event_type?: string;            // "reactive" | "predictive"
  forecast_horizon?: string;      // e.g. "1h"
  predicted_crossing_time?: number; // Unix seconds when trend crosses capacity
  confidence?: number;            // R² [0,1]
  threshold_config?: string;      // JSON: {"capacity": N, "metric": "..."}
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
  const raw = localStorage.getItem(CREDS_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Credentials;
  } catch {
    return null;
  }
}

export function saveCredentials(creds: Credentials): void {
  localStorage.setItem(CREDS_KEY, JSON.stringify(creds));
}

export function clearCredentials(): void {
  localStorage.removeItem(CREDS_KEY);
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
  severity?: string
): Promise<AnomalyListResponse> {
  const params = new URLSearchParams();
  if (since !== undefined) params.set("since", String(since));
  if (severity) params.set("severity", severity);
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

// ---- VictoriaMetrics instant query ----------------------------------------
// Proxied through the analysis service at GET /api/v1/query?q=<promql>.
// Does NOT go through the LLM — sub-10ms, zero token budget consumed.

export interface InstantSample {
  labels: Record<string, string>;
  value: number;
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
