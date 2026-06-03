package shortener

import (
	"context"
	"time"
)

// Task is a unit of work pushed onto the analytics queue when a redirect fires.
type Task struct {
	Code      string
	Timestamp time.Time
}

// Queue is a buffered channel queue backed by a goroutine worker pool.
type Queue struct {
	ch      chan Task
	metrics *Metrics
	store   *Store
}

// NewQueue creates a Queue with a channel of the given capacity.
func NewQueue(capacity int, m *Metrics, s *Store) *Queue {
	return &Queue{
		ch:      make(chan Task, capacity),
		metrics: m,
		store:   s,
	}
}

// Enqueue attempts to push a task onto the channel without blocking.
// Returns false (and increments the dropped counter) if the channel is full.
func (q *Queue) Enqueue(t Task) bool {
	select {
	case q.ch <- t:
		q.metrics.AnalyticsQueueDepth.Set(float64(len(q.ch)))
		return true
	default:
		q.metrics.TasksProcessedTotal.WithLabelValues("dropped").Inc()
		return false
	}
}

// Start spawns n worker goroutines that drain the queue until ctx is cancelled.
func (q *Queue) Start(ctx context.Context, n int) {
	for i := 0; i < n; i++ {
		go q.run(ctx)
	}
}

func (q *Queue) run(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case t := <-q.ch:
			q.metrics.AnalyticsQueueDepth.Set(float64(len(q.ch)))
			q.process(t)
		}
	}
}

func (q *Queue) process(t Task) {
	start := time.Now()
	q.store.IncrClick(t.Code)
	q.metrics.TaskDurationSeconds.Observe(time.Since(start).Seconds())
	q.metrics.TasksProcessedTotal.WithLabelValues("ok").Inc()
}
