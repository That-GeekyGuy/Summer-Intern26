package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"bess.internal/upf-detector/internal/detector"
	"bess.internal/upf-detector/internal/metrics"
	"bess.internal/upf-detector/internal/notifier"
	"bess.internal/upf-detector/internal/store"
	"bess.internal/upf-detector/internal/vmclient"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"gopkg.in/yaml.v3"
)

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	rulesPath := env("RULES_FILE", "/etc/detection/rules.yml")
	data, err := os.ReadFile(rulesPath)
	if err != nil {
		log.Error("failed to read rules file", "path", rulesPath, "err", err)
		os.Exit(1)
	}
	var cfg detector.Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		log.Error("failed to parse rules file", "err", err)
		os.Exit(1)
	}

	pollInterval, err := time.ParseDuration(env("POLL_INTERVAL", cfg.PollingInterval))
	if err != nil || pollInterval == 0 {
		pollInterval = 60 * time.Second
	}
	window, err := time.ParseDuration(env("WINDOW", cfg.Window))
	if err != nil || window == 0 {
		window = 30 * time.Minute
	}
	retentionDays, _ := strconv.Atoi(env("RETENTION_DAYS", "30"))
	if retentionDays <= 0 {
		retentionDays = 30
	}

	db, err := store.Open(env("SQLITE_PATH", "/data/anomalies.db"))
	if err != nil {
		log.Error("failed to open database", "err", err)
		os.Exit(1)
	}
	defer db.Close()

	vm := vmclient.New(
		env("VM_URL", "http://victoriametrics:8428"),
		env("VM_AUTH_USERNAME", ""),
		env("VM_AUTH_PASSWORD", ""),
	)

	// ── Prometheus metrics ────────────────────────────────────────────────────
	reg := prometheus.NewRegistry()
	reg.MustRegister(prometheus.NewGoCollector(), prometheus.NewProcessCollector(prometheus.ProcessCollectorOpts{}))
	m := metrics.New(reg)

	det := detector.New(vm, db, cfg.Metrics, window, log).WithMetrics(m)

	// ── Tier 3 forecaster (optional) ─────────────────────────────────────────
	forecaster, err := detector.NewForecaster(vm, db, cfg.Forecast, log)
	if err != nil {
		log.Error("failed to create forecaster", "err", err)
		os.Exit(1)
	}
	if forecaster != nil {
		log.Info("tier-3 forecaster enabled",
			"targets", len(cfg.Forecast.Targets),
			"horizon", cfg.Forecast.Horizon,
		)
	}

	// ── Notification dispatcher (optional) ───────────────────────────────────
	var disp *notifier.Dispatcher
	hooks := []notifier.ActionHook{&notifier.NoOpHook{Log: log}}
	if webhookURL := env("NOTIFY_WEBHOOK_URL", ""); webhookURL != "" {
		hooks = append(hooks, notifier.NewScaleHintWebhook(webhookURL, log))
		log.Info("scale-hint webhook configured", "url", webhookURL)
	}
	resWindow, _ := time.ParseDuration(env("NOTIFY_RESOLUTION_WINDOW", "5m"))
	disp = notifier.NewDispatcher(hooks, resWindow, log)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()

	// Prune old events daily.
	go func() {
		ticker := time.NewTicker(24 * time.Hour)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				n, err := db.Prune(ctx, retentionDays)
				if err != nil {
					log.Error("prune failed", "err", err)
				} else {
					log.Info("pruned old anomaly events", "deleted", n)
				}
			}
		}
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"status": "ok"}) //nolint:errcheck
	})
	mux.HandleFunc("GET /anomalies", handleAnomalies(db, log))
	mux.Handle("GET /metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))

	srv := &http.Server{Addr: env("HTTP_ADDR", ":8081"), Handler: mux}
	go func() {
		log.Info("detection HTTP server starting", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error("HTTP server error", "err", err)
		}
	}()

	log.Info("detection service started",
		"poll_interval", pollInterval, "window", window, "metrics", len(cfg.Metrics))

	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			log.Info("shutting down")
			shutCtx, shutCancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer shutCancel()
			srv.Shutdown(shutCtx) //nolint:errcheck
			return
		case <-ticker.C:
			// Reactive detection (Tier 1 / Tier 2)
			reactive, err := det.Run(ctx)
			if err != nil {
				log.Error("detection run failed", "err", err)
			}

			// Predictive forecasting (Tier 3) — fires before capacity breach
			var predictive []store.Event
			if forecaster != nil {
				predictive, err = forecaster.Run(ctx)
				if err != nil {
					log.Error("forecast run failed", "err", err)
				}
			}

			// Dispatch all new events through the notification state machine.
			all := append(reactive, predictive...)
			if len(all) > 0 {
				disp.Notify(ctx, all)
			}
		}
	}
}

func handleAnomalies(db *store.Store, log *slog.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		since := time.Now().Add(-time.Hour)
		if s := r.URL.Query().Get("since"); s != "" {
			if unix, err := strconv.ParseInt(s, 10, 64); err == nil {
				since = time.Unix(unix, 0)
			}
		}
		// ?type=reactive|predictive|all (default: all, empty string means all)
		eventType := r.URL.Query().Get("type")
		if eventType == "all" {
			eventType = ""
		}

		events, err := db.List(r.Context(), since,
			r.URL.Query().Get("metric"),
			r.URL.Query().Get("severity"),
			eventType,
		)
		if err != nil {
			log.Error("anomalies query failed", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		if events == nil {
			events = []store.QueryResult{}
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"anomalies": events, "total": len(events)}) //nolint:errcheck
	}
}
