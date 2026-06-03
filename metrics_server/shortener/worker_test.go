package shortener_test

import (
	"context"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"metrics-query/shortener"
)

func newTestQueue(t *testing.T, cap int) (*shortener.Queue, *shortener.Metrics, *shortener.Store) {
	t.Helper()
	reg := prometheus.NewRegistry()
	m := shortener.NewMetrics(reg)
	s := shortener.NewStore()
	q := shortener.NewQueue(cap, m, s)
	return q, m, s
}

func TestQueue_EnqueueAndProcess(t *testing.T) {
	q, _, s := newTestQueue(t, 10)
	code, _ := s.Shorten("https://example.com")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	q.Start(ctx, 1)

	if ok := q.Enqueue(shortener.Task{Code: code}); !ok {
		t.Fatal("expected enqueue to succeed on non-full channel")
	}

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if clicks, _ := s.Clicks(code); clicks == 1 {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("worker did not process task within 2 seconds")
}

func TestQueue_ReturnsFalseWhenFull(t *testing.T) {
	q, _, s := newTestQueue(t, 0) // zero-capacity channel: always full
	code, _ := s.Shorten("https://example.com")

	if ok := q.Enqueue(shortener.Task{Code: code}); ok {
		t.Fatal("expected Enqueue to return false on a full channel")
	}
}
