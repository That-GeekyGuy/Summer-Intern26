package shortener

import "github.com/prometheus/client_golang/prometheus"

// Metrics holds every Prometheus metric the shortener emits.
// Use NewMetrics to construct; pass a fresh prometheus.NewRegistry() per test.
type Metrics struct {
	HTTPRequestsTotal   *prometheus.CounterVec
	HTTPRequestDuration *prometheus.HistogramVec

	URLsCreatedTotal prometheus.Counter
	RedirectsTotal   *prometheus.CounterVec

	AnalyticsQueueDepth     prometheus.Gauge
	TasksProcessedTotal     *prometheus.CounterVec
	TaskDurationSeconds     prometheus.Histogram
}

func NewMetrics(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		HTTPRequestsTotal: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "http_requests_total",
			Help: "Total HTTP requests by method, path, and status code.",
		}, []string{"method", "path", "status"}),

		HTTPRequestDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "http_request_duration_seconds",
			Help:    "HTTP request latency.",
			Buckets: prometheus.DefBuckets,
		}, []string{"method", "path"}),

		URLsCreatedTotal: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "shortener_urls_created_total",
			Help: "Cumulative number of URLs shortened.",
		}),

		RedirectsTotal: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "shortener_redirects_total",
			Help: "Redirect count per short code.",
		}, []string{"code"}),

		AnalyticsQueueDepth: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "analytics_queue_depth",
			Help: "Number of analytics tasks currently waiting in the channel.",
		}),

		TasksProcessedTotal: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "analytics_tasks_processed_total",
			Help: `Tasks processed by the worker pool. status="ok" or "dropped".`,
		}, []string{"status"}),

		TaskDurationSeconds: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name:    "analytics_task_duration_seconds",
			Help:    "Time each worker takes to process one analytics task.",
			Buckets: []float64{.0001, .0005, .001, .005, .01, .05, .1},
		}),
	}

	reg.MustRegister(
		m.HTTPRequestsTotal,
		m.HTTPRequestDuration,
		m.URLsCreatedTotal,
		m.RedirectsTotal,
		m.AnalyticsQueueDepth,
		m.TasksProcessedTotal,
		m.TaskDurationSeconds,
	)
	return m
}
