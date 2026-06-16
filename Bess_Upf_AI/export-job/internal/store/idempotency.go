package store

import (
	"context"
	"fmt"
	"strings"
	"time"
)

// manifestKey returns the S3 key for the idempotency marker for metricName on date.
// Stored under _manifests/ to keep it separate from data partitions and allow
// easy wildcard listing or bulk deletion when forcing a full re-export.
func manifestKey(metricName, date string) string {
	return fmt.Sprintf("_manifests/%s/date=%s.done", metricName, date)
}

// ParquetKey returns the S3 object key for the Parquet file for metricName on date.
// Partitioned as {metric_name}/date={YYYY-MM-DD}/{YYYY-MM-DD}.parquet so that
// downstream Spark/DuckDB jobs can use partition pruning directly.
func ParquetKey(metricName string, date time.Time) string {
	d := date.Format("2006-01-02")
	return fmt.Sprintf("%s/date=%s/%s.parquet", metricName, d, d)
}

// IsExported reports whether metricName has already been exported for date.
// A manifest marker at _manifests/{metric}/date={d}.done signals completion.
func IsExported(ctx context.Context, s ObjectStore, bucket, metricName string, date time.Time) (bool, error) {
	return s.ObjectExists(ctx, bucket, manifestKey(metricName, date.Format("2006-01-02")))
}

// MarkExported writes a zero-byte manifest marker.
// Call this only after the Parquet file has been durably uploaded.
// If this write fails, the next run will re-export and overwrite the Parquet
// file — safe because PutObject is atomic in S3-compatible stores.
func MarkExported(ctx context.Context, s ObjectStore, bucket, metricName string, date time.Time) error {
	key := manifestKey(metricName, date.Format("2006-01-02"))
	return s.PutObject(ctx, bucket, key, strings.NewReader(""), 0, "text/plain")
}
