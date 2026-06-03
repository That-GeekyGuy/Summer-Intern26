package shortener_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"metrics-query/shortener"
)

func newTestServer(t *testing.T) *shortener.Server {
	t.Helper()
	reg := prometheus.NewRegistry()
	m := shortener.NewMetrics(reg)
	s := shortener.NewStore()
	q := shortener.NewQueue(10, m, s)
	return shortener.NewServer(s, q, m, reg)
}

func TestHandleShorten_ValidURL(t *testing.T) {
	srv := newTestServer(t)
	body := `{"url":"https://example.com"}`
	req := httptest.NewRequest(http.MethodPost, "/shorten", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()

	srv.Handler().ServeHTTP(rr, req)

	if rr.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d", rr.Code)
	}
	var resp map[string]string
	if err := json.NewDecoder(rr.Body).Decode(&resp); err != nil {
		t.Fatalf("could not decode response: %v", err)
	}
	if len(resp["code"]) != 6 {
		t.Fatalf("expected 6-char code in response, got %q", resp["code"])
	}
}

func TestHandleShorten_MissingURL(t *testing.T) {
	srv := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/shorten", strings.NewReader(`{}`))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()

	srv.Handler().ServeHTTP(rr, req)

	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rr.Code)
	}
}

func TestHandleRedirect_KnownCode(t *testing.T) {
	srv := newTestServer(t)

	// First shorten a URL.
	body := `{"url":"https://example.com"}`
	req := httptest.NewRequest(http.MethodPost, "/shorten", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)

	var resp map[string]string
	json.NewDecoder(rr.Body).Decode(&resp)
	code := resp["code"]

	// Now hit the redirect endpoint.
	req2 := httptest.NewRequest(http.MethodGet, "/"+code, nil)
	rr2 := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr2, req2)

	if rr2.Code != http.StatusFound {
		t.Fatalf("expected 302, got %d", rr2.Code)
	}
	if loc := rr2.Header().Get("Location"); loc != "https://example.com" {
		t.Fatalf("expected redirect to https://example.com, got %q", loc)
	}
}

func TestHandleRedirect_UnknownCode(t *testing.T) {
	srv := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/xxxxxx", nil)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)

	if rr.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rr.Code)
	}
}

func TestHandleStats_KnownCode(t *testing.T) {
	srv := newTestServer(t)

	// Shorten a URL.
	body := `{"url":"https://example.com"}`
	req := httptest.NewRequest(http.MethodPost, "/shorten", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)
	var resp map[string]string
	json.NewDecoder(rr.Body).Decode(&resp)
	code := resp["code"]

	// Check stats.
	req2 := httptest.NewRequest(http.MethodGet, "/stats/"+code, nil)
	rr2 := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr2, req2)

	if rr2.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rr2.Code)
	}
	var stats map[string]interface{}
	json.NewDecoder(rr2.Body).Decode(&stats)
	if stats["code"] != code {
		t.Fatalf("expected code %q in stats, got %v", code, stats["code"])
	}
}

func TestHandleStats_UnknownCode(t *testing.T) {
	srv := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/stats/xxxxxx", nil)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, req)

	if rr.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rr.Code)
	}
}
