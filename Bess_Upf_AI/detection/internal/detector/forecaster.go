package detector

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"time"

	"bess.internal/upf-detector/internal/store"
	"bess.internal/upf-detector/internal/vmclient"
)

// Forecaster runs OLS linear regression over recent metric windows and emits
// Tier 3 predictive events before reactive Tier 1 rules would trigger.
// It fires when the extrapolated trend is projected to breach a capacity ceiling
// within the configured horizon, or when the drop rate is accelerating.
type Forecaster struct {
	vm      *vmclient.Client
	db      *store.Store
	cfg     *ForecastConfig
	window  time.Duration
	horizon time.Duration
	log     *slog.Logger
}

// NewForecaster creates a Forecaster from a validated ForecastConfig.
func NewForecaster(vm *vmclient.Client, db *store.Store, cfg *ForecastConfig, log *slog.Logger) (*Forecaster, error) {
	if cfg == nil || !cfg.Enabled {
		return nil, nil //nolint:nilnil
	}
	window, err := time.ParseDuration(cfg.Window)
	if err != nil {
		return nil, fmt.Errorf("invalid forecast window %q: %w", cfg.Window, err)
	}
	horizon, err := time.ParseDuration(cfg.Horizon)
	if err != nil {
		return nil, fmt.Errorf("invalid forecast horizon %q: %w", cfg.Horizon, err)
	}
	return &Forecaster{vm: vm, db: db, cfg: cfg, window: window, horizon: horizon, log: log}, nil
}

// Run performs one forecast pass and returns newly inserted predictive events.
func (f *Forecaster) Run(ctx context.Context) ([]store.Event, error) {
	now := time.Now()
	start := now.Add(-f.window)
	var inserted []store.Event

	for _, target := range f.cfg.Targets {
		events, err := f.evalTarget(ctx, target, start, now)
		if err != nil {
			f.log.Error("forecast eval failed", "metric", target.Metric, "err", err)
			continue
		}
		inserted = append(inserted, events...)
	}
	return inserted, nil
}

func (f *Forecaster) evalTarget(ctx context.Context, t ForecastTarget, start, now time.Time) ([]store.Event, error) {
	query := t.PromQL
	if query == "" {
		query = fmt.Sprintf(`%s{job="upf"}`, t.Metric)
	}

	series, err := f.vm.QueryRange(ctx, query, start, now, 30*time.Second)
	if err != nil {
		return nil, fmt.Errorf("query %q: %w", query, err)
	}

	var inserted []store.Event
	for _, s := range series {
		if len(s.Values) < 5 {
			continue // insufficient data for a reliable regression
		}

		labelsJSON, _ := json.Marshal(s.Labels)
		labelsStr := string(labelsJSON)

		a, b, r2 := olsLinearFit(s.Values, s.Timestamps)
		if math.IsNaN(a) || math.IsNaN(b) {
			continue
		}

		x_now := float64(now.UnixMilli()-s.Timestamps[0]) / 1000.0
		x_horizon := x_now + f.horizon.Seconds()
		predictedAtHorizon := a + b*x_horizon
		currentValue := s.Values[len(s.Values)-1]

		sev := t.Severity
		if sev == "" {
			sev = SeverityMedium
		}

		var shouldFire bool
		if t.AccelOnly {
			// Drop-rate acceleration: fire if slope is positive (rate is growing).
			shouldFire = b > 1e-10 && currentValue > 0
		} else if t.Capacity > 0 {
			shouldFire = predictedAtHorizon >= t.Capacity
		}

		if !shouldFire {
			continue
		}

		// Dedup: suppress if we emitted a predictive event for this series recently.
		if f.db.HasRecentPrediction(ctx, t.Metric, labelsStr, f.horizon/2) {
			f.log.Debug("suppressing duplicate forecast", "metric", t.Metric)
			continue
		}

		// Compute the time at which the trend line crosses the capacity ceiling.
		var crossingUnix *int64
		if t.Capacity > 0 && b > 0 {
			// Solve: a + b*x_cross = capacity  →  x_cross = (capacity - a) / b
			xCross := (t.Capacity - a) / b
			if xCross > x_now { // crossing is in the future
				crossingTime := s.Timestamps[0]/1000 + int64(xCross)
				crossingUnix = &crossingTime
			}
		}

		threshCfg, _ := json.Marshal(map[string]any{
			"capacity": t.Capacity,
			"metric":   t.Metric,
		})

		ev := store.Event{
			MetricName:            t.Metric,
			Labels:                labelsStr,
			Timestamp:             now,
			ObservedValue:         currentValue,
			ExpectedValue:         &predictedAtHorizon,
			DeviationMagnitude:    math.Abs(predictedAtHorizon - currentValue),
			RuleName:              "forecast_linear",
			Severity:              sev,
			EventType:             "predictive",
			ForecastHorizon:       f.cfg.Horizon,
			PredictedCrossingTime: crossingUnix,
			Confidence:            &r2,
			ThresholdConfig:       string(threshCfg),
		}

		if err := f.db.Insert(ctx, ev); err != nil {
			f.log.Error("failed to store predictive event", "metric", t.Metric, "err", err)
			continue
		}

		f.log.Info("predictive event emitted",
			"metric", t.Metric,
			"current", currentValue,
			"predicted_at_horizon", predictedAtHorizon,
			"horizon", f.cfg.Horizon,
			"r_squared", fmt.Sprintf("%.2f", r2),
		)
		inserted = append(inserted, ev)
	}
	return inserted, nil
}

// olsLinearFit fits y = a + b*x using OLS where x is elapsed seconds since the first sample.
// Returns (intercept, slope, R²). NaN values indicate a degenerate series.
func olsLinearFit(values []float64, timestamps []int64) (a, b, r2 float64) {
	n := float64(len(values))
	if n < 2 {
		return math.NaN(), math.NaN(), 0
	}

	var sumX, sumY, sumXY, sumX2 float64
	t0 := float64(timestamps[0]) / 1000.0

	for i, v := range values {
		x := (float64(timestamps[i]) / 1000.0) - t0
		sumX += x
		sumY += v
		sumXY += x * v
		sumX2 += x * x
	}

	denom := n*sumX2 - sumX*sumX
	if math.Abs(denom) < 1e-10 {
		return math.NaN(), math.NaN(), 0
	}

	b = (n*sumXY - sumX*sumY) / denom
	a = (sumY - b*sumX) / n

	meanY := sumY / n
	var ssTot, ssRes float64
	for i, v := range values {
		x := (float64(timestamps[i]) / 1000.0) - t0
		diff := v - meanY
		ssTot += diff * diff
		res := v - (a + b*x)
		ssRes += res * res
	}
	if ssTot < 1e-10 {
		r2 = 0 // constant series: R² undefined, report zero confidence
	} else {
		r2 = 1 - ssRes/ssTot
	}
	return a, b, r2
}
