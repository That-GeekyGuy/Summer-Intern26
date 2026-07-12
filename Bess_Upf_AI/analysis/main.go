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
	"bess.internal/upf-analysis/internal/chclient"
	"bess.internal/upf-analysis/internal/llm"
	"bess.internal/upf-analysis/internal/metrics"
	"bess.internal/upf-analysis/internal/rag"
	"bess.internal/upf-analysis/internal/simclient"
	"bess.internal/upf-analysis/internal/store"
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
	var logLevel slog.Level
	if level := env("LOG_LEVEL", "INFO"); level != "" {
		if err := logLevel.UnmarshalText([]byte(level)); err != nil {
			logLevel = slog.LevelInfo
		}
	}
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: logLevel}))

	// ── SQLite audit log ──────────────────────────────────────────────────────
	audit, err := store.OpenAuditLog(env("SQLITE_PATH", "/data/audit.db"))
	if err != nil {
		log.Error("failed to open audit database", "err", err)
		os.Exit(1)
	}
	defer audit.Close()

	// ── ClickHouse client ────────────────────────────────────────────────
	ch := chclient.New(
		env("CLICKHOUSE_DSN", "http://clickhouse:8123"),
	)

	// ── Metric-name allowlist ──────────────────────────────────────────────────
	// Not needed for ClickHouse (no dynamic allowlist implementation currently exists)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()

	// ── PromQL validator ──────────────────────────────────────────────────────
	// Not needed for Clickhouse
	// val := validator.New(...)

	// ── RAG retriever (keyword search over docs) ──────────────────────────────
	var ragRetriever rag.Retriever
	docsDir := env("RAG_DOCS_DIR", "/docs")
	if r, err := rag.NewKeywordRetriever(docsDir); err != nil {
		log.Warn("failed to load RAG docs", "dir", docsDir, "err", err)
	} else {
		ragRetriever = r
		log.Info("RAG docs loaded", "dir", docsDir)
	}

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
		env("VLLM_MODEL", "Qwen/Qwen2.5-7B-Instruct"),
	)
	orch := llm.NewOrchestrator(llm.OrchestratorConfig{
		LLM:        llmClient,
		Validator:  nil,
		CH:         ch,
		Allowlist:  nil,
		RAG:        ragRetriever,
		Audit:      audit,
		SessionTTL: mustDuration(env("SESSION_TTL", "30m"), 30*time.Minute),
		MaxIter:    4,
		Log:        log,
		Metrics:    m,
	})
	orch.StartSessionCleanup(ctx)

	// ── RCA Engine (Brain 2 structured root cause analysis) ──────────────────────────
	// Wraps llmClient + ch + validator for anomaly-specific structured completion.
	rcaEngine := llm.NewRCAEngine(llmClient, ch, nil, log)
	log.Info("RCA engine initialised")

	// ── HTTP server ───────────────────────────────────────────────────────────
	rl := api.NewRateLimiter(600, time.Minute)
	h := api.NewHandler(orch, rcaEngine, sim, ch, nil, m, reg, log,
		env("MODELS_DIR", "/models"),
		env("CHRONOS_URL", "http://chronos:8084"),
		audit,
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
