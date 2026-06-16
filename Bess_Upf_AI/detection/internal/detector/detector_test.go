package detector

import (
	"testing"
	"time"
)

// makeTS produces n Unix-millisecond timestamps spaced 15 s apart, ending near now.
func makeTS(n int) []int64 {
	base := time.Now().Add(-time.Duration(n) * 15 * time.Second).UnixMilli()
	ts := make([]int64, n)
	for i := range ts {
		ts[i] = base + int64(i)*15_000
	}
	return ts
}

// ────────────────────────────────── ZScore ──────────────────────────────────

func TestZScore_FlatSeries_NoAnomaly(t *testing.T) {
	r := NewZScoreRule(&ZScoreConfig{Enabled: true, Threshold: 3.0, Severity: SeverityHigh})
	events, err := r.Eval([]float64{100, 100, 100, 100, 100}, makeTS(5))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("flat series: want 0 events, got %d", len(events))
	}
}

func TestZScore_SingleSample_NoAnomaly(t *testing.T) {
	r := NewZScoreRule(&ZScoreConfig{Enabled: true, Threshold: 3.0, Severity: SeverityHigh})
	events, err := r.Eval([]float64{100}, makeTS(1))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("single sample: want 0 events, got %d", len(events))
	}
}

func TestZScore_Spike_DetectsAnomaly(t *testing.T) {
	r := NewZScoreRule(&ZScoreConfig{Enabled: true, Threshold: 3.0, Severity: SeverityHigh})
	values := []float64{100, 101, 99, 100, 102, 98, 100, 101, 99, 500}
	events, err := r.Eval(values, makeTS(len(values)))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) == 0 {
		t.Fatal("spike: expected anomaly, got none")
	}
	if events[0].RuleName != "zscore" {
		t.Errorf("rule name: want %q, got %q", "zscore", events[0].RuleName)
	}
	if events[0].ExpectedValue == nil {
		t.Error("ExpectedValue must be set")
	}
}

func TestZScore_NormalVariation_NoAnomaly(t *testing.T) {
	r := NewZScoreRule(&ZScoreConfig{Enabled: true, Threshold: 3.0, Severity: SeverityHigh})
	values := []float64{100, 102, 98, 101, 99, 103, 97, 100}
	events, err := r.Eval(values, makeTS(len(values)))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("normal variation: want 0 events, got %d", len(events))
	}
}

func TestZScore_SeverityPropagated(t *testing.T) {
	r := NewZScoreRule(&ZScoreConfig{Enabled: true, Threshold: 2.0, Severity: SeverityCritical})
	values := []float64{100, 101, 99, 100, 102, 98, 100, 101, 99, 500}
	events, err := r.Eval(values, makeTS(len(values)))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) > 0 && events[0].Severity != SeverityCritical {
		t.Errorf("severity: want %q, got %q", SeverityCritical, events[0].Severity)
	}
}

// ────────────────────────────────── Trend ──────────────────────────────────

func TestTrend_TooFewSamples_NoAnomaly(t *testing.T) {
	r := NewTrendRule(&TrendConfig{Enabled: true, DeviationThreshold: 0.25, Severity: SeverityMedium})
	events, err := r.Eval([]float64{100, 101}, makeTS(2))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("2 samples: want 0 events, got %d", len(events))
	}
}

func TestTrend_LinearSeries_NoAnomaly(t *testing.T) {
	r := NewTrendRule(&TrendConfig{Enabled: true, DeviationThreshold: 0.25, Severity: SeverityMedium})
	values := []float64{100, 105, 110, 115, 120}
	events, err := r.Eval(values, makeTS(len(values)))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("linear series: want 0 events, got %d", len(events))
	}
}

func TestTrend_SuddenJump_DetectsAnomaly(t *testing.T) {
	r := NewTrendRule(&TrendConfig{Enabled: true, DeviationThreshold: 0.25, Severity: SeverityMedium})
	values := []float64{100, 100, 100, 100, 100, 200}
	events, err := r.Eval(values, makeTS(len(values)))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) == 0 {
		t.Fatal("sudden jump: expected anomaly, got none")
	}
	if events[0].RuleName != "trend" {
		t.Errorf("rule name: want %q, got %q", "trend", events[0].RuleName)
	}
}

func TestTrend_SuddenDrop_DetectsAnomaly(t *testing.T) {
	r := NewTrendRule(&TrendConfig{Enabled: true, DeviationThreshold: 0.25, Severity: SeverityMedium})
	values := []float64{100, 100, 100, 100, 100, 0}
	events, err := r.Eval(values, makeTS(len(values)))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) == 0 {
		t.Fatal("sudden drop: expected anomaly, got none")
	}
}

func TestTrend_EmptySeries_NoAnomaly(t *testing.T) {
	r := NewTrendRule(&TrendConfig{Enabled: true, DeviationThreshold: 0.25, Severity: SeverityMedium})
	events, err := r.Eval(nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("empty series: want 0 events, got %d", len(events))
	}
}

// ──────────────────────────────── Threshold ─────────────────────────────────

func TestThreshold_BelowMin_DetectsAnomaly(t *testing.T) {
	min := 10.0
	r := NewThresholdRule(&ThresholdConfig{Min: &min, Severity: SeverityCritical})
	events, err := r.Eval([]float64{100, 90, 50, 9}, makeTS(4))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) == 0 {
		t.Fatal("below min: expected anomaly, got none")
	}
	if events[0].Severity != SeverityCritical {
		t.Errorf("severity: want %q, got %q", SeverityCritical, events[0].Severity)
	}
}

func TestThreshold_AboveMax_DetectsAnomaly(t *testing.T) {
	max := 100.0
	r := NewThresholdRule(&ThresholdConfig{Max: &max, Severity: SeverityHigh})
	events, err := r.Eval([]float64{50, 75, 101}, makeTS(3))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) == 0 {
		t.Fatal("above max: expected anomaly, got none")
	}
}

func TestThreshold_WithinBounds_NoAnomaly(t *testing.T) {
	min, max := 0.0, 100.0
	r := NewThresholdRule(&ThresholdConfig{Min: &min, Max: &max, Severity: SeverityHigh})
	events, err := r.Eval([]float64{50, 75, 99}, makeTS(3))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("within bounds: want 0 events, got %d", len(events))
	}
}

func TestThreshold_EmptyValues_NoAnomaly(t *testing.T) {
	max := 100.0
	r := NewThresholdRule(&ThresholdConfig{Max: &max, Severity: SeverityHigh})
	events, err := r.Eval(nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("empty values: want 0 events, got %d", len(events))
	}
}

func TestThreshold_ExactBoundary_NoAnomaly(t *testing.T) {
	min, max := 0.0, 100.0
	r := NewThresholdRule(&ThresholdConfig{Min: &min, Max: &max, Severity: SeverityHigh})
	// Strict < / > means exact boundary values do not fire.
	events, err := r.Eval([]float64{0, 100}, makeTS(2))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("exact boundary: want 0 events, got %d", len(events))
	}
}

func TestThreshold_OnlyMinSet_IgnoresHighValues(t *testing.T) {
	min := 5.0
	r := NewThresholdRule(&ThresholdConfig{Min: &min, Severity: SeverityMedium})
	// Very high value should not fire when only min is configured.
	events, err := r.Eval([]float64{1000}, makeTS(1))
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Errorf("only-min, high value: want 0 events, got %d", len(events))
	}
}
