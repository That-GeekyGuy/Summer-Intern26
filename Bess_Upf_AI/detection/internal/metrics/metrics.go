// Package metrics holds all Prometheus metric declarations for the detection service.
package metrics

import "github.com/prometheus/client_golang/prometheus"

// M holds all instrumented metrics for the detection service.
type M struct {
	PollErrors   prometheus.Counter
	AnomalyTotal *prometheus.CounterVec
	EvalLatency  *prometheus.HistogramVec
	StoredEvents prometheus.Gauge
}

// New registers and returns all detection metrics with reg.
func New(reg prometheus.Registerer) *M {
	m := &M{
		PollErrors: prometheus.NewCounter(prometheus.CounterOpts{
			Namespace: "detection",
			Name:      "poll_errors_total",
			Help:      "Total number of VM poll errors.",
		}),
		AnomalyTotal: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: "detection",
			Name:      "anomalies_total",
			Help:      "Total anomaly events detected, by rule and severity.",
		}, []string{"rule", "severity"}),
		EvalLatency: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Namespace: "detection",
			Name:      "rule_eval_duration_seconds",
			Help:      "Rule evaluation latency per metric series.",
			Buckets:   prometheus.DefBuckets,
		}, []string{"rule", "metric"}),
		StoredEvents: prometheus.NewGauge(prometheus.GaugeOpts{
			Namespace: "detection",
			Name:      "sqlite_stored_events",
			Help:      "Current number of anomaly events in the SQLite store.",
		}),
	}
	reg.MustRegister(m.PollErrors, m.AnomalyTotal, m.EvalLatency, m.StoredEvents)
	return m
}
