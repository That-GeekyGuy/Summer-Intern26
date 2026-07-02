package api

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	detclient "bess.internal/upf-analysis/internal/detector"
	"bess.internal/upf-analysis/internal/llm"
	"bess.internal/upf-analysis/internal/metrics"
	"bess.internal/upf-analysis/internal/simclient"
	"bess.internal/upf-analysis/internal/store"
	"bess.internal/upf-analysis/internal/temporal"
	"bess.internal/upf-analysis/internal/validator"
	"bess.internal/upf-analysis/internal/vmclient"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// ChatRequest is the request body for POST /api/v1/chat.
type ChatRequest struct {
	SessionID string `json:"session_id"`
	Message   string `json:"message"`
}

// ChatResponse is the HTTP response body for POST /api/v1/chat.
// Anomalies is populated so the frontend reasoning trail can show referenced events.
type ChatResponse struct {
	SessionID    string                   `json:"session_id"`
	Answer       string                   `json:"answer"`
	QueriesUsed  []string                 `json:"queries_used"`
	AnomalyCount int                      `json:"anomaly_count"`
	Anomalies    []detclient.AnomalyEvent `json:"anomalies,omitempty"`
}

const maxChatMessageBytes = 4096

// Handler wires all HTTP routes.
type Handler struct {
	orch       *llm.Orchestrator
	rca        *llm.RCAEngine
	det        *detclient.Client
	sim        *simclient.Client
	vm         *vmclient.Client
	temp       *temporal.Client     // nil-safe — optional STL sidecar
	val        *validator.Validator // used to validate frontend /query requests
	m          *metrics.M           // nil-safe
	reg        prometheus.Gatherer
	log        *slog.Logger
	modelsDir   string
	chronosURL  string
	chronosHTTP *http.Client
	audit       *store.AuditLog // nil-safe
}

func NewHandler(orch *llm.Orchestrator, rca *llm.RCAEngine, det *detclient.Client, sim *simclient.Client, vm *vmclient.Client, temp *temporal.Client, val *validator.Validator, m *metrics.M, reg prometheus.Gatherer, log *slog.Logger, modelsDir, chronosURL string, audit *store.AuditLog) *Handler {
	return &Handler{
		orch: orch, rca: rca, det: det, sim: sim, vm: vm, temp: temp, val: val, m: m, reg: reg, log: log,
		modelsDir:   modelsDir,
		chronosURL:  chronosURL,
		chronosHTTP: &http.Client{Timeout: 30 * time.Second},
		audit:       audit,
	}
}

// Register mounts all routes onto mux.
func (h *Handler) Register(mux *http.ServeMux, authUser, authPass string, rl *RateLimiter) {
	mux.HandleFunc("GET /health", h.handleHealth)
	mux.Handle("GET /metrics", promhttp.HandlerFor(h.reg, promhttp.HandlerOpts{}))

	auth := func(next http.Handler) http.Handler {
		return BasicAuth(authUser, authPass, RateLimit(rl, next))
	}

	mux.Handle("POST /api/v1/chat", auth(http.HandlerFunc(h.handleChat)))
	mux.Handle("GET /api/v1/query", auth(http.HandlerFunc(h.handleQuery)))
	mux.Handle("GET /api/v1/pulse", auth(http.HandlerFunc(h.handlePulse)))
	mux.Handle("GET /api/v1/anomalies", auth(http.HandlerFunc(h.handleAnomalies)))
	mux.Handle("GET /api/v1/scenario", auth(http.HandlerFunc(h.handleScenarioGet)))
	mux.Handle("POST /api/v1/scenario", auth(http.HandlerFunc(h.handleScenarioPost)))
	mux.Handle("GET /api/v1/health", auth(http.HandlerFunc(h.handleAggregatedHealth)))

	// Temporal intelligence endpoints — routed through Caddy, require auth.
	mux.Handle("GET /api/v1/temporal/analysis", auth(http.HandlerFunc(h.handleTemporalAnalysis)))
	mux.Handle("GET /api/v1/temporal/hotzone", auth(http.HandlerFunc(h.handleTemporalHotzone)))

	// Structured audit log — last N LLM/tool interactions (auth-guarded, truncated).
	mux.Handle("GET /api/v1/audit", auth(http.HandlerFunc(h.handleAudit)))

	// Ablation benchmark report — static JSON produced by tools/train/ablation.py.
	mux.Handle("GET /api/v1/benchmark", auth(http.HandlerFunc(h.handleBenchmark)))

	// Chronos-2 uncertainty intervals proxy — calls internal Chronos sidecar.
	mux.Handle("GET /api/v1/intervals", auth(http.HandlerFunc(h.handleIntervals)))

	// Internal endpoint: called by detection service only, not routed through Caddy.
	// No auth — this path is only reachable on the internal Docker network.
	mux.HandleFunc("POST /internal/analyze_anomaly", h.handleAnalyzeAnomaly)
}

func (h *Handler) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"}) //nolint:errcheck
}

func (h *Handler) handleAggregatedHealth(w http.ResponseWriter, r *http.Request) {
    // Basic implementation: check if components are non-nil for status, 
    // real implementation would ping them.
	status := map[string]string{
		"analysis": "ok",
		"prometheus": "ok",
		"vm": "ok",
		"detection": "ok",
		"llm": "ok",
		"sim": "ok",
	}
	if h.vm == nil { status["vm"] = "down" }
	if h.det == nil { status["detection"] = "down" }
	if h.sim == nil { status["sim"] = "down" }
	if h.rca == nil { status["llm"] = "down" }

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(status) //nolint:errcheck
}

func (h *Handler) handlePulse(w http.ResponseWriter, r *http.Request) {
    ctx := r.Context()
    sessions, _ := h.vm.QueryInstant(ctx, `sum(pfcp_sessions_total{job="upf"})`)
    n3rx, _ := h.vm.QueryInstant(ctx, `sum(rate(port_bytes_count{job="upf",dir="rx",iface="N3"}[1m]))`)
    n6tx, _ := h.vm.QueryInstant(ctx, `sum(rate(port_bytes_count{job="upf",dir="tx",iface="N6"}[1m]))`)
    drops, _ := h.vm.QueryInstant(ctx, `sum(rate(port_dropped_count{job="upf"}[1m]))`)
    
    sumValues := func(samples []vmclient.InstantSample) float64 {
        sum := 0.0
        for _, s := range samples {
            sum += s.Value
        }
        return sum
    }

    w.Header().Set("Content-Type", "application/json")
    json.NewEncoder(w).Encode(map[string]float64{
        "sessions": sumValues(sessions),
        "n3rx":     sumValues(n3rx),
        "n6tx":     sumValues(n6tx),
        "drops":    sumValues(drops),
    })
}

func (h *Handler) handleChat(w http.ResponseWriter, r *http.Request) {
	var req ChatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON body", http.StatusBadRequest)
		return
	}
	if strings.TrimSpace(req.Message) == "" {
		http.Error(w, `{"error":"message is required"}`, http.StatusBadRequest)
		return
	}
	if len(req.Message) > maxChatMessageBytes {
		http.Error(w, `{"error":"message exceeds maximum length"}`, http.StatusRequestEntityTooLarge)
		return
	}

	sessionID := req.SessionID
	if sessionID == "" {
		sessionID = newSessionID()
	}

	start := time.Now()
	resp, err := h.orch.Chat(r.Context(), sessionID, req.Message)
	if h.m != nil {
		h.m.ChatDuration.Observe(time.Since(start).Seconds())
	}
	if err != nil {
		h.log.Error("chat error", "session", sessionID, "err", err)
		http.Error(w, `{"error":"internal error processing request"}`, http.StatusInternalServerError)
		return
	}

	queriesUsed := resp.QueriesUsed
	if queriesUsed == nil {
		queriesUsed = []string{}
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(ChatResponse{ //nolint:errcheck
		SessionID:    sessionID,
		Answer:       resp.Answer,
		QueriesUsed:  queriesUsed,
		AnomalyCount: len(resp.Anomalies),
		Anomalies:    resp.Anomalies,
	})
}

// handleQuery proxies a PromQL instant query to VictoriaMetrics and returns
// the raw samples. Used by the frontend pulse strip to read metrics directly
// without routing through the LLM chat endpoint.
// Query param: q (required) — a PromQL expression, e.g. pfcp_sessions_total
func (h *Handler) handleQuery(w http.ResponseWriter, r *http.Request) {
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	if q == "" {
		http.Error(w, `{"error":"q parameter is required"}`, http.StatusBadRequest)
		return
	}
	// Validate against allowlist and policy limits before forwarding to VictoriaMetrics.
	// Instant queries use a 0 time range and 0 step — validator only checks the AST.
	if h.val != nil {
		if err := h.val.Validate(q, 0, 0); err != nil {
			h.log.Warn("handleQuery rejected", "q", q, "reason", err)
			b, _ := json.Marshal(map[string]string{"error": "query rejected by policy: " + err.Error()})
			http.Error(w, string(b), http.StatusForbidden)
			return
		}
	}
	samples, err := h.vm.QueryInstant(r.Context(), q)
	if err != nil {
		h.log.Error("vm instant query failed", "q", q, "err", err)
		http.Error(w, `{"error":"VictoriaMetrics query failed"}`, http.StatusBadGateway)
		return
	}
	if samples == nil {
		samples = []vmclient.InstantSample{}
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
		"query":   q,
		"samples": samples,
	})
}

// handleAnomalies proxies to the detection service's /anomalies endpoint.
// Query params: since (unix seconds), metric, severity — forwarded as-is.
func (h *Handler) handleAnomalies(w http.ResponseWriter, r *http.Request) {
	since := time.Hour // default: last hour
	if s := r.URL.Query().Get("since"); s != "" {
		if unix, err := strconv.ParseInt(s, 10, 64); err == nil {
			since = time.Since(time.Unix(unix, 0))
			if since < 0 {
				since = 0
			}
		}
	}

	anomalies, err := h.det.GetAnomalies(
		r.Context(),
		since,
		r.URL.Query().Get("metric"),
		r.URL.Query().Get("severity"),
	)
	if err != nil {
		h.log.Error("anomalies proxy failed", "err", err)
		http.Error(w, `{"error":"detection service unavailable"}`, http.StatusBadGateway)
		return
	}
	if anomalies == nil {
		anomalies = []detclient.AnomalyEvent{}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
		"anomalies": anomalies,
		"total":     len(anomalies),
	})
}

// handleScenarioGet proxies GET /scenario to the upf-sim service.
func (h *Handler) handleScenarioGet(w http.ResponseWriter, r *http.Request) {
	if h.sim == nil {
		http.Error(w, `{"error":"simulator not configured"}`, http.StatusServiceUnavailable)
		return
	}
	status, err := h.sim.Get(r.Context())
	if err != nil {
		h.log.Error("scenario get failed", "err", err)
		http.Error(w, `{"error":"simulator unavailable"}`, http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(status) //nolint:errcheck
}

// handleScenarioPost proxies POST /scenario to the upf-sim service.
func (h *Handler) handleScenarioPost(w http.ResponseWriter, r *http.Request) {
	if h.sim == nil {
		http.Error(w, `{"error":"simulator not configured"}`, http.StatusServiceUnavailable)
		return
	}
	status, err := h.sim.Set(r.Context(), r.Body)
	if err != nil {
		h.log.Error("scenario set failed", "err", err)
		http.Error(w, `{"error":"simulator unavailable"}`, http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(status) //nolint:errcheck
}

func newSessionID() string {
	b := make([]byte, 16)
	rand.Read(b) //nolint:errcheck
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// handleTemporalAnalysis proxies to the STL sidecar's /analysis endpoint (60s cache in client).
// Returns the full temporal context: calendar, current regime, peak/trough hours.
func (h *Handler) handleTemporalAnalysis(w http.ResponseWriter, r *http.Request) {
	if h.temp == nil {
		http.Error(w, `{"error":"temporal sidecar not configured"}`, http.StatusServiceUnavailable)
		return
	}
	analysis, err := h.temp.GetAnalysis(r.Context())
	if err != nil {
		h.log.Warn("temporal analysis unavailable", "err", err)
		http.Error(w, `{"error":"temporal sidecar unavailable"}`, http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(analysis) //nolint:errcheck
}

// handleTemporalHotzone proxies to the STL sidecar's /hotzone endpoint (1h cache in client).
// Returns the 24-hour expected regime forecast for capacity planning.
func (h *Handler) handleTemporalHotzone(w http.ResponseWriter, r *http.Request) {
	if h.temp == nil {
		http.Error(w, `{"error":"temporal sidecar not configured"}`, http.StatusServiceUnavailable)
		return
	}
	hotzone, err := h.temp.GetHotzone(r.Context())
	if err != nil {
		h.log.Warn("temporal hotzone unavailable", "err", err)
		http.Error(w, `{"error":"temporal sidecar unavailable"}`, http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(hotzone) //nolint:errcheck
}

// handleAnalyzeAnomaly is an INTERNAL endpoint — not routed through Caddy or exposed to the internet.
// It is called by the detection service after a MOMENT anomaly event to request LLM-generated RCA.
// POST /internal/analyze_anomaly
func (h *Handler) handleAnalyzeAnomaly(w http.ResponseWriter, r *http.Request) {
	if h.rca == nil {
		http.Error(w, `{"error":"RCA engine not configured"}`, http.StatusServiceUnavailable)
		return
	}

	var req llm.RCARequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `{"error":"invalid JSON body"}`, http.StatusBadRequest)
		return
	}

	report, err := h.rca.Analyze(r.Context(), req)
	if err != nil {
		h.log.Error("RCA analysis failed", "err", err)
		http.Error(w, `{"error":"RCA analysis failed"}`, http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{"rca_report": report}) //nolint:errcheck
}

// handleAudit returns recent structured audit log entries.
// GET /api/v1/audit?limit=N&since=<unix>&tool=<name>
// Returns last N LLM/tool interactions. user_message and tool_args are truncated to 512 chars.
func (h *Handler) handleAudit(w http.ResponseWriter, r *http.Request) {
	if h.audit == nil {
		http.Error(w, `{"error":"audit log not configured"}`, http.StatusServiceUnavailable)
		return
	}
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	since, _  := strconv.ParseInt(r.URL.Query().Get("since"), 10, 64)
	tool       := r.URL.Query().Get("tool")

	entries, total, err := h.audit.Query(r.Context(), limit, since, tool)
	if err != nil {
		h.log.Error("audit query failed", "err", err)
		http.Error(w, `{"error":"audit query failed"}`, http.StatusInternalServerError)
		return
	}
	if entries == nil {
		entries = []store.AuditRow{}
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
		"entries": entries,
		"total":   total,
	})
}

// handleBenchmark serves the ablation evaluation report written by tools/train/ablation.py.
// The file is parsed and re-serialised (never streamed raw) to ensure only valid JSON is returned.
// Security: this protects against a malformed file containing unexpected keys or values.
func (h *Handler) handleBenchmark(w http.ResponseWriter, r *http.Request) {
	path := filepath.Join(h.modelsDir, "ablation_report.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusNotFound)
			json.NewEncoder(w).Encode(map[string]any{ //nolint:errcheck
				"available": false,
				"message":   "ablation report not found — run tools/train/ablation.py first",
			})
			return
		}
		h.log.Error("benchmark read failed", "path", path, "err", err)
		http.Error(w, `{"error":"internal error reading benchmark report"}`, http.StatusInternalServerError)
		return
	}
	var report map[string]any
	if err := json.Unmarshal(data, &report); err != nil {
		h.log.Error("benchmark parse failed", "err", err)
		http.Error(w, `{"error":"benchmark report is malformed JSON"}`, http.StatusInternalServerError)
		return
	}
	report["available"] = true
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(report) //nolint:errcheck
}

// handleIntervals proxies a Chronos-2 uncertainty interval request.
// It fetches recent VM data for the requested PromQL expression (context array)
// then POSTs to the Chronos /intervals endpoint and returns P10/P50/P90 bands.
// GET /api/v1/intervals?q=<promql>&horizon=<short|medium|long>
func (h *Handler) handleIntervals(w http.ResponseWriter, r *http.Request) {
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	if q == "" {
		http.Error(w, `{"error":"q parameter required"}`, http.StatusBadRequest)
		return
	}
	// Validate promql against allowlist before forwarding.
	if h.val != nil {
		if err := h.val.Validate(q, 0, 0); err != nil {
			http.Error(w, `{"error":"query rejected by policy"}`, http.StatusForbidden)
			return
		}
	}
	horizon := r.URL.Query().Get("horizon")
	if horizon == "" {
		horizon = "short"
	}

	// Fetch last 60 minutes at 60s steps → up to 60 context points.
	vals, err := h.vm.QueryRawValues(r.Context(), q, 60*time.Minute, 60*time.Second)
	if err != nil || len(vals) == 0 {
		// Chronos needs at least some context; fall back gracefully.
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"available": false, "channel": q}) //nolint:errcheck
		return
	}

	body, _ := json.Marshal(map[string]any{
		"channel": q,
		"context": vals,
		"horizon": horizon,
	})

	chronosReq, err := http.NewRequestWithContext(r.Context(), http.MethodPost,
		h.chronosURL+"/intervals", bytes.NewReader(body))
	if err != nil {
		http.Error(w, `{"error":"chronos request failed"}`, http.StatusInternalServerError)
		return
	}
	chronosReq.Header.Set("Content-Type", "application/json")

	resp, err := h.chronosHTTP.Do(chronosReq)
	if err != nil {
		h.log.Warn("chronos sidecar unavailable", "err", err)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"available": false, "channel": q}) //nolint:errcheck
		return
	}
	defer resp.Body.Close()

	var chronosResp map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&chronosResp); err != nil {
		http.Error(w, `{"error":"invalid response from chronos"}`, http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(chronosResp) //nolint:errcheck
}


