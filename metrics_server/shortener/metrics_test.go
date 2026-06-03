package shortener_test

import (
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"metrics-query/shortener"
)

func TestNewMetrics_RegistersWithoutPanic(t *testing.T) {
	reg := prometheus.NewRegistry()
	m := shortener.NewMetrics(reg)
	if m == nil {
		t.Fatal("expected non-nil Metrics")
	}
}

func TestNewMetrics_PanicsOnDoubleRegister(t *testing.T) {
	reg := prometheus.NewRegistry()
	shortener.NewMetrics(reg)
	defer func() {
		if r := recover(); r == nil {
			t.Error("expected panic on double registration")
		}
	}()
	shortener.NewMetrics(reg) // same registry → must panic
}
