package store_test

import (
	"context"
	"io"
	"sync"
	"testing"
	"time"

	"bess.internal/upf-exporter/internal/store"
)

// memStore is a thread-safe in-memory ObjectStore for unit tests.
type memStore struct {
	mu      sync.RWMutex
	objects map[string]bool
}

func newMemStore() *memStore {
	return &memStore{objects: make(map[string]bool)}
}

func (m *memStore) ObjectExists(_ context.Context, _, key string) (bool, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.objects[key], nil
}

func (m *memStore) PutObject(_ context.Context, _, key string, _ io.Reader, _ int64, _ string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.objects[key] = true
	return nil
}

var (
	testBucket = "upf-metrics"
	testMetric = "upf_pdu_sessions_total"
	testDate   = time.Date(2024, 1, 15, 0, 0, 0, 0, time.UTC)
)

func TestIsExported_ReturnsFalseWhenNoMarker(t *testing.T) {
	s := newMemStore()
	got, err := store.IsExported(context.Background(), s, testBucket, testMetric, testDate)
	if err != nil {
		t.Fatal(err)
	}
	if got {
		t.Error("IsExported = true before any MarkExported call")
	}
}

func TestMarkThenIsExported_ReturnsTrue(t *testing.T) {
	s := newMemStore()
	if err := store.MarkExported(context.Background(), s, testBucket, testMetric, testDate); err != nil {
		t.Fatal("MarkExported:", err)
	}
	got, err := store.IsExported(context.Background(), s, testBucket, testMetric, testDate)
	if err != nil {
		t.Fatal("IsExported:", err)
	}
	if !got {
		t.Error("IsExported = false after MarkExported")
	}
}

func TestIsExported_DifferentDateIsIndependent(t *testing.T) {
	s := newMemStore()
	d2 := testDate.AddDate(0, 0, 1)

	if err := store.MarkExported(context.Background(), s, testBucket, testMetric, testDate); err != nil {
		t.Fatal(err)
	}

	got, err := store.IsExported(context.Background(), s, testBucket, testMetric, d2)
	if err != nil {
		t.Fatal(err)
	}
	if got {
		t.Errorf("marking %s leaked into %s", testDate.Format("2006-01-02"), d2.Format("2006-01-02"))
	}
}

func TestIsExported_DifferentMetricIsIndependent(t *testing.T) {
	s := newMemStore()
	other := "upf_bytes_transmitted_total"

	if err := store.MarkExported(context.Background(), s, testBucket, testMetric, testDate); err != nil {
		t.Fatal(err)
	}

	got, err := store.IsExported(context.Background(), s, testBucket, other, testDate)
	if err != nil {
		t.Fatal(err)
	}
	if got {
		t.Errorf("marking %q leaked into %q", testMetric, other)
	}
}

func TestParquetKey_Format(t *testing.T) {
	key := store.ParquetKey("upf_pdu_sessions_total", testDate)
	want := "upf_pdu_sessions_total/date=2024-01-15/2024-01-15.parquet"
	if key != want {
		t.Errorf("ParquetKey = %q, want %q", key, want)
	}
}

func TestMarkExported_IsIdempotent(t *testing.T) {
	s := newMemStore()
	for i := 0; i < 3; i++ {
		if err := store.MarkExported(context.Background(), s, testBucket, testMetric, testDate); err != nil {
			t.Fatalf("MarkExported call %d: %v", i, err)
		}
	}
	got, err := store.IsExported(context.Background(), s, testBucket, testMetric, testDate)
	if err != nil || !got {
		t.Errorf("IsExported after repeated MarkExported: got=%v err=%v", got, err)
	}
}
