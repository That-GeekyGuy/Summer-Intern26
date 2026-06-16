package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"bess.internal/upf-sim/internal/api"
	"bess.internal/upf-sim/internal/sim"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envFloat(key string, fallback float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return fallback
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	cfg := sim.Config{
		NodeID:          env("NODE_ID", "sim-upf-01"),
		BaseSessions:    envFloat("BASE_SESSIONS", 12_000),
		BytesPerSession: envFloat("BYTES_PER_SESSION", 1_000),
		AvgPacketSize:   envFloat("AVG_PACKET_SIZE", 800),
		BaseDropRate:    envFloat("BASE_DROP_RATE", 0.0001),
		SpikeMultiplier: envFloat("SPIKE_MULTIPLIER", 4.0),
		RampSeconds:     envFloat("RAMP_SECONDS", 120),
	}

	reg := prometheus.NewRegistry()
	engine := sim.NewEngine(cfg, reg, log)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()

	// ── Optional sequence scheduler ───────────────────────────────────────────
	if seqFile := env("SEQUENCE_FILE", ""); seqFile != "" {
		seqCfg, err := sim.LoadSequence(seqFile)
		if err != nil {
			log.Error("failed to load sequence file", "path", seqFile, "err", err)
			os.Exit(1)
		}
		sched := sim.NewScheduler(engine, seqCfg, log)
		go sched.Run(ctx)
		log.Info("sequence scheduler started", "file", seqFile)
	}

	// ── Simulation tick loop (1 s resolution) ─────────────────────────────────
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				engine.Tick(now)
			}
		}
	}()

	// ── HTTP server ───────────────────────────────────────────────────────────
	mux := http.NewServeMux()

	// /metrics — Prometheus scrape endpoint (custom registry; no Go runtime metrics)
	mux.Handle("GET /metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{
		EnableOpenMetrics: true,
	}))

	// /scenario — GET = current state, POST = change mode
	mux.HandleFunc("GET /scenario", api.HandleScenarioGet(engine))
	mux.HandleFunc("POST /scenario", api.HandleScenarioPost(engine, log))

	// /healthz — liveness probe
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`)) //nolint:errcheck
	})

	srv := &http.Server{
		Addr:         env("HTTP_ADDR", ":8090"),
		Handler:      mux,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 15 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		log.Info("upf-sim HTTP server starting",
			"addr", srv.Addr,
			"node_id", cfg.NodeID,
			"base_sessions", cfg.BaseSessions)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("HTTP server error", "err", err)
		}
	}()

	<-ctx.Done()
	log.Info("shutting down upf-sim")
	shutCtx, shutCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutCancel()
	srv.Shutdown(shutCtx) //nolint:errcheck
}
