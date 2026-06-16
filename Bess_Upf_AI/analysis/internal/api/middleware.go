package api

import (
	"crypto/subtle"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// BasicAuth returns middleware that validates HTTP Basic credentials.
func BasicAuth(user, password string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u, p, ok := r.BasicAuth()
		if !ok ||
			subtle.ConstantTimeCompare([]byte(u), []byte(user)) != 1 ||
			subtle.ConstantTimeCompare([]byte(p), []byte(password)) != 1 {
			w.Header().Set("WWW-Authenticate", `Basic realm="upf-analysis"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// bucket is a fixed-window token bucket for one IP address.
type bucket struct {
	tokens  int
	resetAt time.Time
}

// RateLimiter enforces a per-IP request rate using a fixed window.
type RateLimiter struct {
	mu      sync.Mutex
	buckets map[string]*bucket
	limit   int
	window  time.Duration
}

// NewRateLimiter creates a limiter allowing limit requests per window per IP.
func NewRateLimiter(limit int, window time.Duration) *RateLimiter {
	return &RateLimiter{
		buckets: make(map[string]*bucket),
		limit:   limit,
		window:  window,
	}
}

// Allow returns true if the request from ip is within the rate limit.
func (rl *RateLimiter) Allow(ip string) bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	now := time.Now()
	b, ok := rl.buckets[ip]
	if !ok || now.After(b.resetAt) {
		rl.buckets[ip] = &bucket{tokens: rl.limit - 1, resetAt: now.Add(rl.window)}
		return true
	}
	if b.tokens <= 0 {
		return false
	}
	b.tokens--
	return true
}

// RateLimit returns middleware that enforces rl on requests.
// Uses X-Forwarded-For (set by Caddy) so each real client gets its own bucket,
// rather than all traffic sharing the proxy's internal IP.
func RateLimit(rl *RateLimiter, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ip := clientIP(r)
		if !rl.Allow(ip) {
			http.Error(w, "rate limit exceeded", http.StatusTooManyRequests)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// clientIP extracts the originating client IP, preferring the first value in
// X-Forwarded-For (set by Caddy) over the raw RemoteAddr (which is Caddy's
// internal container IP when proxied).
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		// XFF may be a comma-separated list; take the leftmost (originating) IP.
		if idx := strings.IndexByte(xff, ','); idx != -1 {
			return strings.TrimSpace(xff[:idx])
		}
		return strings.TrimSpace(xff)
	}
	ip, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return ip
}

// RequestLogger returns middleware that logs every request.
func RequestLogger(log *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rw := &responseWriter{ResponseWriter: w, code: http.StatusOK}
		start := time.Now()
		next.ServeHTTP(rw, r)
		log.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rw.code,
			"duration_ms", time.Since(start).Milliseconds(),
		)
	})
}

type responseWriter struct {
	http.ResponseWriter
	code int
}

func (rw *responseWriter) WriteHeader(code int) {
	rw.code = code
	rw.ResponseWriter.WriteHeader(code)
}
