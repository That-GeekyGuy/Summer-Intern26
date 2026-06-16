package detector

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"bess.internal/upf-detector/internal/metrics"
	"bess.internal/upf-detector/internal/store"
	"bess.internal/upf-detector/internal/vmclient"
)

// Detector polls VictoriaMetrics on a schedule and runs all configured rules.
type Detector struct {
	vm      *vmclient.Client
	store   *store.Store
	metrics []MetricConfig
	window  time.Duration
	log     *slog.Logger
	m       *metrics.M // nil-safe: instrumentation is optional
}

func New(vm *vmclient.Client, st *store.Store, metricCfgs []MetricConfig, window time.Duration, log *slog.Logger) *Detector {
	return &Detector{vm: vm, store: st, metrics: metricCfgs, window: window, log: log}
}

// WithMetrics attaches Prometheus instrumentation to the detector.
func (d *Detector) WithMetrics(m *metrics.M) *Detector {
	d.m = m
	return d
}

// Run performs one detection pass over all configured metrics.
// It continues past per-metric errors, returns all newly inserted events,
// and the first error encountered.
func (d *Detector) Run(ctx context.Context) ([]store.Event, error) {
	end := time.Now()
	start := end.Add(-d.window)
	var firstErr error
	var inserted []store.Event

	for _, mc := range d.metrics {
		events, err := d.evalMetric(ctx, mc, start, end)
		if err != nil {
			d.log.Error("metric evaluation failed", "metric", mc.Name, "err", err)
			if d.m != nil {
				d.m.PollErrors.Inc()
			}
			if firstErr == nil {
				firstErr = err
			}
		}
		inserted = append(inserted, events...)
	}
	return inserted, firstErr
}

func (d *Detector) evalMetric(ctx context.Context, mc MetricConfig, start, end time.Time) ([]store.Event, error) {
	query := mc.Name
	if mc.Selector != "" {
		query = mc.Name + mc.Selector
	}

	series, err := d.vm.QueryRange(ctx, query, start, end, 15*time.Second)
	if err != nil {
		return nil, fmt.Errorf("query %q: %w", query, err)
	}

	var inserted []store.Event
	rules := rulesForConfig(mc.Rules)
	for _, s := range series {
		labelsJSON, _ := json.Marshal(s.Labels)
		for _, rule := range rules {
			var evalStart time.Time
			if d.m != nil {
				evalStart = time.Now()
			}

			events, err := rule.Eval(s.Values, s.Timestamps)

			if d.m != nil {
				d.m.EvalLatency.WithLabelValues(rule.Name(), mc.Name).
					Observe(time.Since(evalStart).Seconds())
			}

			if err != nil {
				d.log.Warn("rule eval error", "rule", rule.Name(), "metric", mc.Name, "err", err)
				continue
			}
			for i := range events {
				events[i].MetricName = mc.Name
				events[i].Labels = s.Labels
				ev := store.Event{
					MetricName:         events[i].MetricName,
					Labels:             string(labelsJSON),
					Timestamp:          events[i].Timestamp,
					ObservedValue:      events[i].ObservedValue,
					ExpectedValue:      events[i].ExpectedValue,
					DeviationMagnitude: events[i].DeviationMagnitude,
					RuleName:           events[i].RuleName,
					Severity:           events[i].Severity,
				}
				if err := d.store.Insert(ctx, ev); err != nil {
					d.log.Error("failed to store anomaly event", "err", err)
				} else {
					inserted = append(inserted, ev)
					if d.m != nil {
						d.m.AnomalyTotal.WithLabelValues(events[i].RuleName, events[i].Severity).Inc()
						d.m.StoredEvents.Inc()
					}
				}
			}
		}
	}
	return inserted, nil
}

func rulesForConfig(rs RuleSet) []Rule {
	var rules []Rule
	if rs.ZScore != nil && rs.ZScore.Enabled {
		rules = append(rules, NewZScoreRule(rs.ZScore))
	}
	if rs.Trend != nil && rs.Trend.Enabled {
		rules = append(rules, NewTrendRule(rs.Trend))
	}
	if rs.Threshold != nil && (rs.Threshold.Min != nil || rs.Threshold.Max != nil) {
		rules = append(rules, NewThresholdRule(rs.Threshold))
	}
	return rules
}
