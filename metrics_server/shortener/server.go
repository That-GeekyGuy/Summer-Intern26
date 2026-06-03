package shortener

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Server wires together the store, queue, and metrics into HTTP handlers.
type Server struct {
	store   *Store
	queue   *Queue
	metrics *Metrics
	handler http.Handler
}

func NewServer(s *Store, q *Queue, m *Metrics, reg prometheus.Gatherer) *Server {
	srv := &Server{store: s, queue: q, metrics: m}

	mux := http.NewServeMux()
	mux.HandleFunc("POST /shorten", srv.handleShorten)
	mux.HandleFunc("GET /stats/{code}", srv.handleStats)
	mux.HandleFunc("GET /{code}", srv.handleRedirect)
	mux.Handle("GET /metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))

	srv.handler = srv.instrument(mux)
	return srv
}

// Handler returns the fully instrumented HTTP handler.
func (s *Server) Handler() http.Handler { return s.handler }

// responseWriter wraps http.ResponseWriter to capture the status code
// after WriteHeader is called — the standard library doesn't expose it.
type responseWriter struct {
	http.ResponseWriter
	status int
}

func (rw *responseWriter) WriteHeader(code int) {
	rw.status = code
	rw.ResponseWriter.WriteHeader(code)
}

// instrument wraps every request with Prometheus timing and counting.
// Path labels use the mux pattern (e.g. "/{code}") not the actual URL,
// preventing high cardinality from unique short codes.
func (s *Server) instrument(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &responseWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)

		pattern := r.Pattern // populated by net/http after Go 1.22 routing
		if pattern == "" {
			pattern = r.URL.Path
		}
		status := strconv.Itoa(rw.status)
		s.metrics.HTTPRequestsTotal.WithLabelValues(r.Method, pattern, status).Inc()
		s.metrics.HTTPRequestDuration.WithLabelValues(r.Method, pattern).Observe(time.Since(start).Seconds())
	})
}

func (s *Server) handleShorten(w http.ResponseWriter, r *http.Request) {
	var body struct {
		URL string `json:"url"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.URL == "" {
		http.Error(w, `{"error":"url is required"}`, http.StatusBadRequest)
		return
	}

	code, err := s.store.Shorten(body.URL)
	if err != nil {
		http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
		return
	}

	s.metrics.URLsCreatedTotal.Inc()

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(map[string]string{
		"code":      code,
		"short_url": fmt.Sprintf("http://%s/%s", r.Host, code),
	})
}

func (s *Server) handleRedirect(w http.ResponseWriter, r *http.Request) {
	code := r.PathValue("code")
	originalURL, ok := s.store.Get(code)
	if !ok {
		http.NotFound(w, r)
		return
	}

	s.metrics.RedirectsTotal.WithLabelValues(code).Inc()
	s.queue.Enqueue(Task{Code: code, Timestamp: time.Now()})

	http.Redirect(w, r, originalURL, http.StatusFound)
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	code := r.PathValue("code")
	clicks, ok := s.store.Clicks(code)
	if !ok {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"code":   code,
		"clicks": clicks,
	})
}
