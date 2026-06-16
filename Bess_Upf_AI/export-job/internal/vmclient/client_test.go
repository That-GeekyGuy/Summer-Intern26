package vmclient_test

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"bess.internal/upf-exporter/internal/vmclient"
)

func TestExportMetric_ParsesNDJSON(t *testing.T) {
	body := `{"metric":{"__name__":"upf_pdu_sessions_total","instance":"10.0.0.1:9090","job":"upf"},"values":[1,2,3],"timestamps":[1000,2000,3000]}
{"metric":{"__name__":"upf_pdu_sessions_total","instance":"10.0.0.2:9090","job":"upf"},"values":[10,20,30],"timestamps":[1000,2000,3000]}
`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/export" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if got := r.URL.Query().Get("match[]"); got != "upf_pdu_sessions_total" {
			t.Errorf("match[] param = %q, want %q", got, "upf_pdu_sessions_total")
		}
		u, p, ok := r.BasicAuth()
		if !ok || u != "vmuser" || p != "vmpass" {
			t.Errorf("basic auth: ok=%v user=%q (want vmuser)", ok, u)
		}
		fmt.Fprint(w, body)
	}))
	defer srv.Close()

	c := vmclient.New(srv.URL, "vmuser", "vmpass")
	series, err := c.ExportMetric(context.Background(),
		"upf_pdu_sessions_total",
		time.Unix(0, 0), time.Unix(4, 0),
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(series) != 2 {
		t.Fatalf("got %d series, want 2", len(series))
	}
	if len(series[0].Values) != 3 {
		t.Errorf("series[0]: got %d values, want 3", len(series[0].Values))
	}
	if series[0].Metric["instance"] != "10.0.0.1:9090" {
		t.Errorf("series[0] instance = %q", series[0].Metric["instance"])
	}
	if series[0].Values[1] != 2 {
		t.Errorf("series[0].Values[1] = %v, want 2", series[0].Values[1])
	}
}

func TestExportMetric_NonOKStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	c := vmclient.New(srv.URL, "", "")
	_, err := c.ExportMetric(context.Background(), "m", time.Unix(0, 0), time.Unix(1, 0))
	if err == nil {
		t.Fatal("expected error for HTTP 401, got nil")
	}
}

func TestExportMetric_EmptyBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c := vmclient.New(srv.URL, "", "")
	series, err := c.ExportMetric(context.Background(), "no_data_metric", time.Unix(0, 0), time.Unix(1, 0))
	if err != nil {
		t.Fatal(err)
	}
	if len(series) != 0 {
		t.Errorf("got %d series, want 0", len(series))
	}
}

func TestExportMetric_MalformedJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintln(w, `{not valid json`)
	}))
	defer srv.Close()

	c := vmclient.New(srv.URL, "", "")
	_, err := c.ExportMetric(context.Background(), "m", time.Unix(0, 0), time.Unix(1, 0))
	if err == nil {
		t.Fatal("expected error for malformed JSON, got nil")
	}
}
