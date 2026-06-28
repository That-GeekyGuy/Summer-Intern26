package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"bess.internal/upf-analysis/internal/api"
	detclient "bess.internal/upf-analysis/internal/detector"
	"bess.internal/upf-analysis/internal/llm"
	"bess.internal/upf-analysis/internal/metrics"
	"bess.internal/upf-analysis/internal/rag"
	"bess.internal/upf-analysis/internal/simclient"
	"bess.internal/upf-analysis/internal/store"
	"bess.internal/upf-analysis/internal/temporal"
	"bess.internal/upf-analysis/internal/validator"
	"bess.internal/upf-analysis/internal/vmclient"
	"github.com/prometheus/client_golang/prometheus"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func mustDuration(s string, fallback time.Duration) time.Duration {
	d, err := time.ParseDuration(s)
	if err != nil || d <= 0 {
		return fallback
	}
	return d
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	// ── SQLite audit log ──────────────────────────────────────────────────────
	audit, err := store.OpenAuditLog(env("SQLITE_PATH", "/data/audit.db"))
	if err != nil {
		log.Error("failed to open audit database", "err", err)
		os.Exit(1)
	}
	defer audit.Close()

	// ── VictoriaMetrics client ────────────────────────────────────────────────
	vm := vmclient.New(
		env("VM_URL", "http://victoriametrics:8428"),
		env("VM_AUTH_USERNAME", ""),
		env("VM_AUTH_PASSWORD", ""),
	)

	// ── Dynamic allowlist (from VM) ───────────────────────────────────────────
	allowlist := validator.NewDynamicAllowlist(
		env("VM_URL", "http://victoriametrics:8428"),
		env("VM_AUTH_USERNAME", ""),
		env("VM_AUTH_PASSWORD", ""),
	)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()

	// Initial allowlist population — retry up to 3 times, but don't block startup.
	for i := 0; i < 3; i++ {
		if err := allowlist.Refresh(ctx); err != nil {
			log.Warn("allowlist initial refresh failed", "attempt", i+1, "err", err)
			time.Sleep(3 * time.Second)
		} else {
			log.Info("allowlist populated", "metrics", allowlist.Size())
			break
		}
	}
	allowlist.StartRefreshLoop(ctx,
		mustDuration(env("ALLOWLIST_REFRESH_INTERVAL", "5m"), 5*time.Minute),
		log.Warn)

	// ── PromQL validator ──────────────────────────────────────────────────────
	val := validator.New(
		allowlist,
		mustDuration(env("MAX_QUERY_RANGE", "720h"), 720*time.Hour),
		mustDuration(env("MIN_STEP", "15s"), 15*time.Second),
	)

	// ── RAG retriever (keyword search over docs) ──────────────────────────────
	var ragRetriever rag.Retriever
	docsDir := env("RAG_DOCS_DIR", "/docs")
	if r, err := rag.NewKeywordRetriever(docsDir); err != nil {
		log.Warn("failed to load RAG docs", "dir", docsDir, "err", err)
	} else {
		ragRetriever = r
		log.Info("RAG docs loaded", "dir", docsDir)
	}

	// ── Detection service client ──────────────────────────────────────────────
	det := detclient.New(env("DETECTION_URL", "http://detection:8081"))

	// ── Simulator client (optional — nil-safe in handler) ────────────────────
	var sim *simclient.Client
	if simURL := env("SIM_URL", ""); simURL != "" {
		sim = simclient.New(simURL)
		log.Info("upf-sim client configured", "url", simURL)
	} else {
		log.Warn("SIM_URL not set; /api/v1/scenario endpoints will return 503")
	}

	// ── Prometheus metrics ────────────────────────────────────────────────────
	reg := prometheus.NewRegistry()
	reg.MustRegister(prometheus.NewGoCollector(), prometheus.NewProcessCollector(prometheus.ProcessCollectorOpts{}))
	m := metrics.New(reg)

	// ── LLM client + orchestrator ─────────────────────────────────────────────
	llmClient := llm.NewClient(
		env("VLLM_URL", "http://vllm:8000"),
		env("VLLM_MODEL", "Qwen/Qwen3-8B"),
	)
	orch := llm.NewOrchestrator(llm.OrchestratorConfig{
		LLM:        llmClient,
		Validator:  val,
		VM:         vm,
		Detector:   det,
		Allowlist:  allowlist,
		RAG:        ragRetriever,
		Audit:      audit,
		SessionTTL: mustDuration(env("SESSION_TTL", "30m"), 30*time.Minute),
		MaxIter:    4,
		Log:        log,
		Metrics:    m,
	})
	orch.StartSessionCleanup(ctx)

	// ── RCA Engine (Brain 2 structured root cause analysis) ──────────────────────────
	// Wraps llmClient + vm + validator for anomaly-specific structured completion.
	rcaEngine := llm.NewRCAEngine(llmClient, vm, val, log)
	log.Info("RCA engine initialised")

	// ── Temporal intelligence sidecar client (optional) ──────────────────────
	// If STL_URL is not set, temporal endpoints return 503 — non-fatal.
	var tempClient *temporal.Client
	if stlURL := env("STL_URL", ""); stlURL != "" {
		tempClient = temporal.New(stlURL)
		log.Info("temporal sidecar client configured", "url", stlURL)
		// Inject into RCA engine for calendar + correlation context in prompts.
		rcaEngine.WithTemporalClient(tempClient)
	} else {
		log.Warn("STL_URL not set; /api/v1/temporal/* endpoints will return 503")
	}

	// ── HTTP server ───────────────────────────────────────────────────────────
	rl := api.NewRateLimiter(60, time.Minute)
	h := api.NewHandler(orch, rcaEngine, det, sim, vm, tempClient, val, m, reg, log,
		env("MODELS_DIR", "/models"),
		env("CHRONOS_URL", "http://chronos:8084"),
	)
	mux := http.NewServeMux()
	h.Register(mux, env("ANALYSIS_AUTH_USER", "admin"), env("ANALYSIS_AUTH_PASSWORD", ""), rl)

	srv := &http.Server{
		Addr:         env("HTTP_ADDR", ":8082"),
		Handler:      api.RequestLogger(log, mux),
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 180 * time.Second, // long timeout for LLM calls
		IdleTimeout:  120 * time.Second,
	}

	go func() {
		log.Info("analysis HTTP server starting", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("HTTP server error", "err", err)
		}
	}()

	<-ctx.Done()
	log.Info("shutting down")
	shutCtx, shutCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutCancel()
	srv.Shutdown(shutCtx) //nolint:errcheck
}
