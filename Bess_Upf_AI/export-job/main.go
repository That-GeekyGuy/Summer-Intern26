package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"

	"bess.internal/upf-exporter/internal/job"
	"bess.internal/upf-exporter/internal/store"
	"bess.internal/upf-exporter/internal/vmclient"
)

type config struct {
	VMURL          string
	VMUser         string
	VMPassword     string
	MinIOEndpoint  string
	MinIOAccessKey string
	MinIOSecretKey string
	MinioBucket    string
	MinIOUseSSL    bool
	Lookback       time.Duration
	Metrics        []string
}

func configFromEnv() (config, error) {
	lookbackStr := envOr("EXPORT_LOOKBACK", "24h")
	lookback, err := time.ParseDuration(lookbackStr)
	if err != nil {
		return config{}, fmt.Errorf("EXPORT_LOOKBACK=%q: %w", lookbackStr, err)
	}

	metricsStr := os.Getenv("EXPORT_METRICS")
	if metricsStr == "" {
		return config{}, fmt.Errorf("EXPORT_METRICS must be set (comma-separated metric names)")
	}
	var metrics []string
	for _, m := range strings.Split(metricsStr, ",") {
		if t := strings.TrimSpace(m); t != "" {
			metrics = append(metrics, t)
		}
	}

	return config{
		VMURL:          envOr("VM_URL", "http://victoriametrics:8428"),
		VMUser:         os.Getenv("VM_AUTH_USERNAME"),
		VMPassword:     os.Getenv("VM_AUTH_PASSWORD"),
		MinIOEndpoint:  envOr("MINIO_ENDPOINT", "minio:9000"),
		MinIOAccessKey: os.Getenv("MINIO_ACCESS_KEY"),
		MinIOSecretKey: os.Getenv("MINIO_SECRET_KEY"),
		MinioBucket:    envOr("MINIO_BUCKET", "upf-metrics"),
		MinIOUseSSL:    os.Getenv("MINIO_USE_SSL") == "true",
		Lookback:       lookback,
		Metrics:        metrics,
	}, nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	level := slog.LevelInfo
	if strings.EqualFold(os.Getenv("LOG_LEVEL"), "debug") {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})))

	cfg, err := configFromEnv()
	if err != nil {
		slog.Error("invalid configuration", "err", err)
		os.Exit(1)
	}

	vm := vmclient.New(cfg.VMURL, cfg.VMUser, cfg.VMPassword)

	s, err := store.NewMinIOStore(store.MinIOConfig{
		Endpoint:  cfg.MinIOEndpoint,
		AccessKey: cfg.MinIOAccessKey,
		SecretKey: cfg.MinIOSecretKey,
		UseSSL:    cfg.MinIOUseSSL,
	})
	if err != nil {
		slog.Error("failed to create MinIO client", "err", err)
		os.Exit(1)
	}

	j := job.New(vm, s, cfg.MinioBucket)

	// 2-hour wall-clock timeout covers the full export of all configured metrics.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Hour)
	defer cancel()

	end := time.Now().UTC()
	start := end.Add(-cfg.Lookback)

	slog.Info("export run starting",
		"metrics", cfg.Metrics,
		"start", start.Format(time.RFC3339),
		"end", end.Format(time.RFC3339),
	)

	if err := j.Run(ctx, cfg.Metrics, start, end); err != nil {
		slog.Error("export run failed", "err", err)
		os.Exit(1)
	}

	slog.Info("export run complete")
}
