package detector

import (
	"math"
	"time"
)

// ThresholdRule emits an anomaly when the latest value falls outside static bounds.
// Bounds are half-open: a value exactly equal to min or max does not fire.
type ThresholdRule struct {
	min      *float64
	max      *float64
	severity string
}

func NewThresholdRule(cfg *ThresholdConfig) *ThresholdRule {
	sev := SeverityCritical
	if cfg.Severity != "" {
		sev = cfg.Severity
	}
	return &ThresholdRule{min: cfg.Min, max: cfg.Max, severity: sev}
}

func (r *ThresholdRule) Name() string { return "threshold" }

func (r *ThresholdRule) Eval(values []float64, timestamps []int64) ([]AnomalyEvent, error) {
	if len(values) == 0 {
		return nil, nil
	}

	latest := values[len(values)-1]
	ts := time.Unix(timestamps[len(timestamps)-1]/1000, 0)

	var violated bool
	var expected float64
	if r.min != nil && latest < *r.min {
		violated = true
		expected = *r.min
	} else if r.max != nil && latest > *r.max {
		violated = true
		expected = *r.max
	}
	if !violated {
		return nil, nil
	}

	return []AnomalyEvent{{
		Timestamp:          ts,
		ObservedValue:      latest,
		ExpectedValue:      &expected,
		DeviationMagnitude: math.Abs(latest - expected),
		RuleName:           r.Name(),
		Severity:           r.severity,
	}}, nil
}
