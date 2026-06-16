// Package metrics holds all Prometheus metric declarations for the analysis service.
package metrics

import "github.com/prometheus/client_golang/prometheus"

// M holds all instrumented metrics for the analysis service.
type M struct {
	ChatDuration      prometheus.Histogram
	ValidatorRejects  *prometheus.CounterVec
	ToolCalls         *prometheus.CounterVec
	ActiveSessions    prometheus.Gauge
}

// New registers and returns all analysis metrics with reg.
func New(reg prometheus.Registerer) *M {
	m := &M{
		ChatDuration: prometheus.NewHistogram(prometheus.HistogramOpts{
			Namespace: "analysis",
			Name:      "chat_duration_seconds",
			Help:      "End-to-end latency of chat requests (including LLM tool-calling loop).",
			Buckets:   []float64{1, 5, 10, 30, 60, 120, 180},
		}),
		ValidatorRejects: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: "analysis",
			Name:      "validator_rejections_total",
			Help:      "PromQL queries rejected by the validator, by reason category.",
		}, []string{"reason"}),
		ToolCalls: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: "analysis",
			Name:      "tool_calls_total",
			Help:      "LLM tool invocations, by tool name.",
		}, []string{"tool"}),
		ActiveSessions: prometheus.NewGauge(prometheus.GaugeOpts{
			Namespace: "analysis",
			Name:      "active_sessions",
			Help:      "Number of chat sessions currently alive in memory.",
		}),
	}
	reg.MustRegister(m.ChatDuration, m.ValidatorRejects, m.ToolCalls, m.ActiveSessions)
	return m
}
