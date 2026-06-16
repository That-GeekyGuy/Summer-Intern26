package detector

import (
	"math"
	"time"
)

// TrendRule detects sudden departures from the recent linear trend.
// It fits y = a + b*x via OLS over all-but-last samples, where x is the
// sample index (not raw epoch millis) to avoid float64 catastrophic cancellation.
type TrendRule struct {
	deviationThreshold float64
	severity           string
}

func NewTrendRule(cfg *TrendConfig) *TrendRule {
	dt := 0.25
	if cfg.DeviationThreshold > 0 {
		dt = cfg.DeviationThreshold
	}
	sev := SeverityMedium
	if cfg.Severity != "" {
		sev = cfg.Severity
	}
	return &TrendRule{deviationThreshold: dt, severity: sev}
}

func (r *TrendRule) Name() string { return "trend" }

func (r *TrendRule) Eval(values []float64, timestamps []int64) ([]AnomalyEvent, error) {
	n := len(values)
	if n < 3 {
		return nil, nil
	}

	// Fit y = a + b*x on the training window (all but the last point).
	// x is the sample index so t values stay small — avoids float64
	// catastrophic cancellation when raw epoch-millis are squared.
	fit := values[:n-1]
	nf := float64(len(fit))

	var sumX, sumV, sumXV, sumX2 float64
	for i, v := range fit {
		x := float64(i)
		sumX += x
		sumV += v
		sumXV += x * v
		sumX2 += x * x
	}

	denom := nf*sumX2 - sumX*sumX
	if math.Abs(denom) < 1e-10 {
		return nil, nil // degenerate (single point or all-same x)
	}

	b := (nf*sumXV - sumX*sumV) / denom
	a := (sumV - b*sumX) / nf

	// Predict at x = n-1 (the last sample's index).
	predicted := a + b*float64(n-1)
	actual := values[n-1]

	var relDev float64
	if math.Abs(predicted) < 1.0 {
		relDev = math.Abs(actual - predicted)
	} else {
		relDev = math.Abs(actual-predicted) / math.Abs(predicted)
	}

	if relDev <= r.deviationThreshold {
		return nil, nil
	}

	return []AnomalyEvent{{
		Timestamp:          time.Unix(timestamps[n-1]/1000, 0),
		ObservedValue:      actual,
		ExpectedValue:      &predicted,
		DeviationMagnitude: relDev,
		RuleName:           r.Name(),
		Severity:           r.severity,
	}}, nil
}