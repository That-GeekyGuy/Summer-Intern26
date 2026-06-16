package notifier

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"bess.internal/upf-detector/internal/store"
)

var severityRank = map[string]int{
	"low": 1, "medium": 2, "high": 3, "critical": 4,
}

type notifyState struct {
	severity    string
	lastSeen    time.Time
	lastFired   time.Time
}

// Dispatcher applies state-machine deduplication before dispatching events to
// registered ActionHooks. It fires only on state transitions:
//   - NEW: first occurrence of an (event_type, metric, labels) key
//   - ESCALATION: severity increased since last dispatch
//
// Keys not seen for resolutionWindow are evicted and treated as resolved on
// next occurrence.
type Dispatcher struct {
	mu               sync.Mutex
	hooks            []ActionHook
	states           map[string]*notifyState
	resolutionWindow time.Duration
	log              *slog.Logger
}

// NewDispatcher creates a Dispatcher with the given hooks and resolution window.
// If resolutionWindow <= 0, it defaults to 5 minutes.
func NewDispatcher(hooks []ActionHook, resolutionWindow time.Duration, log *slog.Logger) *Dispatcher {
	if resolutionWindow <= 0 {
		resolutionWindow = 5 * time.Minute
	}
	return &Dispatcher{
		hooks:            hooks,
		states:           make(map[string]*notifyState),
		resolutionWindow: resolutionWindow,
		log:              log,
	}
}

// Notify processes a batch of newly detected events and dispatches hooks on
// state transitions. It is safe to call concurrently.
func (d *Dispatcher) Notify(ctx context.Context, events []store.Event) {
	if len(events) == 0 {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()

	now := time.Now()
	for _, ev := range events {
		key := ev.EventType + ":" + ev.MetricName + ":" + ev.Labels
		prev, exists := d.states[key]

		shouldDispatch := !exists ||
			severityRank[ev.Severity] > severityRank[prev.severity]

		if shouldDispatch {
			d.dispatch(ctx, ev)
			d.states[key] = &notifyState{
				severity:  ev.Severity,
				lastSeen:  now,
				lastFired: now,
			}
		} else {
			prev.lastSeen = now
		}
	}

	// Evict keys that have not been seen recently (considered resolved).
	for key, state := range d.states {
		if now.Sub(state.lastSeen) > d.resolutionWindow {
			d.log.Info("event key resolved (no recurrence)", "key", key)
			delete(d.states, key)
		}
	}
}

func (d *Dispatcher) dispatch(ctx context.Context, ev store.Event) {
	for _, h := range d.hooks {
		if err := h.OnEvent(ctx, ev); err != nil {
			d.log.Error("hook dispatch error",
				"hook", fmt.Sprintf("%T", h),
				"metric", ev.MetricName,
				"err", err,
			)
		}
	}
}
