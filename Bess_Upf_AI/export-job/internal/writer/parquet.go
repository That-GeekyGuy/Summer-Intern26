// Package writer encodes VictoriaMetrics series data into Parquet format.
// Schema (wide): one row per sample.
//
//	timestamp_ms int64  — Unix milliseconds
//	metric_name  string — value of the "__name__" label
//	value        float64
//	labels       string — JSON object of all labels (including "__name__")
//
// The wide schema is chosen because the full label set is not known at compile
// time (UPF vendors add labels freely). Downstream Spark/DuckDB jobs can
// parse the JSON column or use get_json_object() for specific label lookups.
package writer

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"

	"bess.internal/upf-exporter/internal/vmclient"
	"github.com/parquet-go/parquet-go"
)

// Row is the Parquet schema. Field tags control column name and codec.
type Row struct {
	TimestampMs int64   `parquet:"timestamp_ms"`
	MetricName  string  `parquet:"metric_name,zstd"`
	Value       float64 `parquet:"value"`
	Labels      string  `parquet:"labels,zstd"` // JSON; includes "__name__"
}

// WriteParquet encodes all series into Parquet and writes the result to w.
// Returns the total number of rows written.
// w must be an io.Writer that buffers or flushes atomically (e.g. *bytes.Buffer
// or an os.File) — Parquet footers are written on Close, so partial writes
// produce an unreadable file.
func WriteParquet(series []vmclient.Series, w io.Writer) (int, error) {
	pw := parquet.NewGenericWriter[Row](w)

	total := 0
	for _, s := range series {
		name := s.Metric["__name__"]

		labelsJSON, err := json.Marshal(s.Metric)
		if err != nil {
			return total, fmt.Errorf("marshal labels for %q: %w", name, err)
		}
		labelStr := string(labelsJSON)

		// Guard against mismatched Timestamps/Values slices from VM.
		n := len(s.Timestamps)
		if len(s.Values) < n {
			n = len(s.Values)
		}

		rows := make([]Row, n)
		for i := 0; i < n; i++ {
			rows[i] = Row{
				TimestampMs: s.Timestamps[i],
				MetricName:  name,
				Value:       s.Values[i],
				Labels:      labelStr,
			}
		}

		if _, err := pw.Write(rows); err != nil {
			return total, fmt.Errorf("write parquet rows for %q: %w", name, err)
		}
		total += n
	}

	if err := pw.Close(); err != nil {
		return total, fmt.Errorf("close parquet writer: %w", err)
	}
	return total, nil
}

// Encode is a convenience wrapper that returns the Parquet bytes in memory.
// Use this when you need the byte count before uploading (required by MinIO
// PutObject for accurate Content-Length).
func Encode(series []vmclient.Series) (data []byte, rows int, err error) {
	buf := new(bytes.Buffer)
	rows, err = WriteParquet(series, buf)
	return buf.Bytes(), rows, err
}
