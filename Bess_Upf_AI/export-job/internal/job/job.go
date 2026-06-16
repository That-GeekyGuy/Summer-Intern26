// Package job orchestrates the export pipeline:
// query VictoriaMetrics → encode Parquet → upload to MinIO → write manifest.
package job

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"time"

	"bess.internal/upf-exporter/internal/store"
	"bess.internal/upf-exporter/internal/vmclient"
	"bess.internal/upf-exporter/internal/writer"
)

// Job holds the dependencies for the export pipeline.
type Job struct {
	vm     *vmclient.Client
	store  store.ObjectStore
	bucket string
}

// New creates a Job.
func New(vm *vmclient.Client, s store.ObjectStore, bucket string) *Job {
	return &Job{vm: vm, store: s, bucket: bucket}
}

// Run exports each metric in metrics for the time window [start, end).
//
// Idempotency: if a manifest marker already exists for a metric+date pair,
// that metric is skipped. To force re-export, delete the marker:
//
//	mc rm local/upf-metrics/_manifests/{metric}/date={date}.done
//
// Partial failures: Run processes all metrics even if some fail, and returns
// the first error encountered. This lets a transient failure for one metric
// not block exports of other metrics. Check logs for per-metric errors.
func (j *Job) Run(ctx context.Context, metrics []string, start, end time.Time) error {
	// Partition key uses the date at the start of the lookback window.
	date := start.UTC().Truncate(24 * time.Hour)

	var firstErr error
	for _, metric := range metrics {
		if err := j.exportOne(ctx, metric, date, start, end); err != nil {
			slog.Error("metric export failed", "metric", metric, "err", err)
			if firstErr == nil {
				firstErr = err
			}
		}
	}
	return firstErr
}

func (j *Job) exportOne(ctx context.Context, metric string, date, start, end time.Time) error {
	log := slog.With("metric", metric, "date", date.Format("2006-01-02"))

	exported, err := store.IsExported(ctx, j.store, j.bucket, metric, date)
	if err != nil {
		return fmt.Errorf("idempotency check: %w", err)
	}
	if exported {
		log.Info("partition already exported, skipping")
		return nil
	}

	log.Info("querying VictoriaMetrics")
	series, err := j.vm.ExportMetric(ctx, metric, start, end)
	if err != nil {
		return fmt.Errorf("query VictoriaMetrics: %w", err)
	}
	if len(series) == 0 {
		log.Warn("no data in window, skipping")
		return nil
	}

	data, nRows, err := writer.Encode(series)
	if err != nil {
		return fmt.Errorf("encode parquet: %w", err)
	}

	key := store.ParquetKey(metric, date)
	log.Info("uploading Parquet", "key", key, "rows", nRows, "bytes", len(data))

	if err := j.store.PutObject(
		ctx, j.bucket, key,
		bytes.NewReader(data), int64(len(data)),
		"application/octet-stream",
	); err != nil {
		return fmt.Errorf("upload to MinIO (key=%s): %w", key, err)
	}

	// Write manifest after the Parquet is durably stored.
	// If this fails, the next run will re-export and overwrite — safe.
	if err := store.MarkExported(ctx, j.store, j.bucket, metric, date); err != nil {
		log.Warn("Parquet uploaded but manifest write failed; next run will re-export",
			"key", key, "err", err)
	}

	log.Info("partition exported", "rows", nRows, "bytes", len(data))
	return nil
}
