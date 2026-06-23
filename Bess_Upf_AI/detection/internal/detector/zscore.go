package detector

import (
	"math"
	"time"
)

// ZScoreRule flags the latest sample when its z-score exceeds the configured threshold.
// Mean and std are computed from the baseline window (all values except the latest)
// so the spike cannot self-suppress by inflating its own distribution.
// A flat baseline (std < 1e-10) is silently skipped.
type ZScoreRule struct {
	threshold float64
	severity  string
}

func NewZScoreRule(cfg *ZScoreConfig) *ZScoreRule {
	t := 3.0
	if cfg.Threshold > 0 {
		t = cfg.Threshold
	}
	sev := SeverityHigh
	if cfg.Severity != "" {
		sev = cfg.Severity
	}
	return &ZScoreRule{threshold: t, severity: sev}
}

func (r *ZScoreRule) Name() string { return "zscore" }

func (r *ZScoreRule) Eval(values []float64, timestamps []int64) ([]AnomalyEvent, error) {
	if len(values) < 2 {
		return nil, nil
	}

	baseline := values[:len(values)-1]
	latest := values[len(values)-1]

	// Require at least 2 baseline samples so the sample std denominator (N-1) is non-zero.
	if len(baseline) < 2 {
		return nil, nil
	}

	mean := 0.0
	for _, v := range baseline {
		mean += v
	}
	mean /= float64(len(baseline))

	variance := 0.0
	for _, v := range baseline {
		d := v - mean
		variance += d * d
	}
	// Use sample std (÷N-1, Bessel correction) — the baseline is a finite window
	// sample, not the full population. Population std (÷N) systematically underestimates
	// the true std, making z-score thresholds harder to breach at small window sizes.
	std := math.Sqrt(variance / float64(len(baseline)-1))
	if std < 1e-10 {
		return nil, nil
	}

	z := (latest - mean) / std
	if math.Abs(z) < r.threshold {
		return nil, nil
	}

	expected := mean
	return []AnomalyEvent{{
		Timestamp:          time.Unix(timestamps[len(timestamps)-1]/1000, 0),
		ObservedValue:      latest,
		ExpectedValue:      &expected,
		DeviationMagnitude: math.Abs(z),
		RuleName:           r.Name(),
		Severity:           r.severity,
	}}, nil
}