package api

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	detclient "bess.internal/upf-analysis/internal/detector"
	"bess.internal/upf-analysis/internal/llm"
	"bess.internal/upf-analysis/internal/metrics"
	"bess.internal/upf-analysis/internal/simclient"
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

// Handler wires all HTTP routes.
type Handler struct {
	orch *llm.Orchestrator
	det  *detclient.Client
	sim  *simclient.Client
	vm   *vmclient.Client
	m    *metrics.M // nil-safe
	reg  prometheus.Gatherer
	log  *slog.Logger
}

func NewHandler(orch *llm.Orchestrator, det *detclient.Client, sim *simclient.Client, vm *vmclient.Client, m *metrics.M, reg prometheus.Gatherer, log *slog.Logger) *Handler {
	return &Handler{orch: orch, det: det, sim: sim, vm: vm, m: m, reg: reg, log: log}
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
	mux.Handle("GET /api/v1/anomalies", auth(http.HandlerFunc(h.handleAnomalies)))
	mux.Handle("GET /api/v1/scenario", auth(http.HandlerFunc(h.handleScenarioGet)))
	mux.Handle("POST /api/v1/scenario", auth(http.HandlerFunc(h.handleScenarioPost)))
}

func (h *Handler) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"}) //nolint:errcheck
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
