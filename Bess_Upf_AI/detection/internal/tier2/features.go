// Package tier2 assembles multivariate feature vectors from VictoriaMetrics
// series and submits them to the ML inference sidecar.
//
// Feature engineering here MUST be kept identical to tools/train/prepare_dataset.py.
// The invariants that must not drift:
//   - Counter reset: value[i] < value[i-1] → rate = NaN (imputed by sidecar)
//   - Gauge rate: simple finite difference / dt
//   - Rolling window: 20 samples (5 min at 15 s cadence), Bessel-corrected std
//   - Feature order: exactly FEATURE_NAMES (exported constant)
package tier2

import (
	"math"

	"bess.internal/upf-detector/internal/vmclient"
)

const (
	scrapeDT      = 15.0  // seconds between scrapes
	rollingWindow = 20    // 5 min at 15 s cadence
	epsilon       = 1e-9
)

// FEATURE_NAMES is the canonical ordered list of features.
// It must match feature_columns.json written by prepare_dataset.py.
var FEATURE_NAMES = []string{
	// Rate features (7)
	"rate_pfcp_sessions",
	"rate_bytes_N3_rx",
	"rate_bytes_N3_tx",
	"rate_bytes_N6_rx",
	"rate_bytes_N6_tx",
	"rate_drops_N3",
	"rate_drops_N6",
	// Ratio features (4)
	"rx_tx_ratio_N3",
	"rx_tx_ratio_N6",
	"drop_fraction_N3",
	"drop_fraction_N6",
	// Rolling stats: mean, std, min, max for each of the 7 rate features (28)
	"rate_pfcp_sessions_mean_5m",
	"rate_pfcp_sessions_std_5m",
	"rate_pfcp_sessions_min_5m",
	"rate_pfcp_sessions_max_5m",
	"rate_bytes_N3_rx_mean_5m",
	"rate_bytes_N3_rx_std_5m",
	"rate_bytes_N3_rx_min_5m",
	"rate_bytes_N3_rx_max_5m",
	"rate_bytes_N3_tx_mean_5m",
	"rate_bytes_N3_tx_std_5m",
	"rate_bytes_N3_tx_min_5m",
	"rate_bytes_N3_tx_max_5m",
	"rate_bytes_N6_rx_mean_5m",
	"rate_bytes_N6_rx_std_5m",
	"rate_bytes_N6_rx_min_5m",
	"rate_bytes_N6_rx_max_5m",
	"rate_bytes_N6_tx_mean_5m",
	"rate_bytes_N6_tx_std_5m",
	"rate_bytes_N6_tx_min_5m",
	"rate_bytes_N6_tx_max_5m",
	"rate_drops_N3_mean_5m",
	"rate_drops_N3_std_5m",
	"rate_drops_N3_min_5m",
	"rate_drops_N3_max_5m",
	"rate_drops_N6_mean_5m",
	"rate_drops_N6_std_5m",
	"rate_drops_N6_min_5m",
	"rate_drops_N6_max_5m",
}

// rate returns the per-second rate for a counter series.
// A decrease (counter reset) returns NaN, matching Python prepare_dataset.py.
func counterRate(prev, cur float64) float64 {
	if math.IsNaN(prev) || math.IsNaN(cur) {
		return math.NaN()
	}
	diff := cur - prev
	if diff < 0 {
		return math.NaN() // counter reset
	}
	return diff / scrapeDT
}

// gaugeRate returns the per-second rate of change for a gauge.
func gaugeRate(prev, cur float64) float64 {
	if math.IsNaN(prev) || math.IsNaN(cur) {
		return math.NaN()
	}
	return (cur - prev) / scrapeDT
}

// rollingStats computes mean, population std (Bessel-corrected, ddof=1), min, max
// over the last min(len(v), rollingWindow) values, ignoring NaN.
// Returns NaN for std when fewer than 2 non-NaN samples exist.
func rollingStats(v []float64) (mean, std, min_, max_ float64) {
	if len(v) == 0 {
		return math.NaN(), math.NaN(), math.NaN(), math.NaN()
	}
	// Use last rollingWindow values
	start := 0
	if len(v) > rollingWindow {
		start = len(v) - rollingWindow
	}
	window := v[start:]

	var sum float64
	min_ = math.Inf(1)
	max_ = math.Inf(-1)
	n := 0
	for _, x := range window {
		if math.IsNaN(x) {
			continue
		}
		sum += x
		if x < min_ {
			min_ = x
		}
		if x > max_ {
			max_ = x
		}
		n++
	}
	if n == 0 {
		return math.NaN(), math.NaN(), math.NaN(), math.NaN()
	}
	mean = sum / float64(n)

	if n < 2 {
		return mean, math.NaN(), min_, max_
	}
	var ssq float64
	for _, x := range window {
		if math.IsNaN(x) {
			continue
		}
		d := x - mean
		ssq += d * d
	}
	std = math.Sqrt(ssq / float64(n-1)) // Bessel correction (ddof=1), matches pandas default
	return mean, std, min_, max_
}

// filterByLabels returns the first series matching all required label key/value pairs.
func filterByLabels(series []vmclient.TimeSeries, required map[string]string) (vmclient.TimeSeries, bool) {
	for _, s := range series {
		match := true
		for k, v := range required {
			if s.Labels[k] != v {
				match = false
				break
			}
		}
		if match {
			return s, true
		}
	}
	return vmclient.TimeSeries{}, false
}

// BuildFeatureVector assembles the 39-element feature vector from raw VM series.
// NaN indicates a missing or uncomputable feature; the sidecar imputes using training mean.
//
// seriesMap keys: "pfcp_sessions", "bytes_rx_N3", "bytes_tx_N3", "bytes_rx_N6", "bytes_tx_N6",
//
//	"pkts_rx_N3", "pkts_rx_N6", "drops_rx_N3", "drops_rx_N6"
//
// Each value is the raw sample slice (oldest→newest) for the last 5 minutes.
func BuildFeatureVector(sm map[string][]float64) map[string]float64 {
	out := make(map[string]float64, len(FEATURE_NAMES))

	// Helper: compute rate series from raw counter values
	counterRates := func(vals []float64) []float64 {
		if len(vals) < 2 {
			return nil
		}
		rates := make([]float64, len(vals)-1)
		for i := 1; i < len(vals); i++ {
			rates[i-1] = counterRate(vals[i-1], vals[i])
		}
		return rates
	}
	gaugeRates := func(vals []float64) []float64 {
		if len(vals) < 2 {
			return nil
		}
		rates := make([]float64, len(vals)-1)
		for i := 1; i < len(vals); i++ {
			rates[i-1] = gaugeRate(vals[i-1], vals[i])
		}
		return rates
	}

	// Rate features
	rfSessions := gaugeRates(sm["pfcp_sessions"])
	rfBytesN3rx := counterRates(sm["bytes_rx_N3"])
	rfBytesN3tx := counterRates(sm["bytes_tx_N3"])
	rfBytesN6rx := counterRates(sm["bytes_rx_N6"])
	rfBytesN6tx := counterRates(sm["bytes_tx_N6"])
	rfDropsN3   := counterRates(sm["drops_rx_N3"])
	rfDropsN6   := counterRates(sm["drops_rx_N6"])
	rfPktsN3rx  := counterRates(sm["pkts_rx_N3"])
	rfPktsN6rx  := counterRates(sm["pkts_rx_N6"])

	nanLast := func(rates []float64) float64 {
		if len(rates) == 0 {
			return math.NaN()
		}
		return rates[len(rates)-1]
	}

	out["rate_pfcp_sessions"] = nanLast(rfSessions)
	out["rate_bytes_N3_rx"]   = nanLast(rfBytesN3rx)
	out["rate_bytes_N3_tx"]   = nanLast(rfBytesN3tx)
	out["rate_bytes_N6_rx"]   = nanLast(rfBytesN6rx)
	out["rate_bytes_N6_tx"]   = nanLast(rfBytesN6tx)
	out["rate_drops_N3"]      = nanLast(rfDropsN3)
	out["rate_drops_N6"]      = nanLast(rfDropsN6)

	// Ratio features
	ratN3rx := out["rate_bytes_N3_rx"]
	ratN3tx := out["rate_bytes_N3_tx"]
	ratN6rx := out["rate_bytes_N6_rx"]
	ratN6tx := out["rate_bytes_N6_tx"]
	dropsN3 := out["rate_drops_N3"]
	dropsN6 := out["rate_drops_N6"]
	pktsN3  := nanLast(rfPktsN3rx)
	pktsN6  := nanLast(rfPktsN6rx)

	safeDivide := func(num, den float64) float64 {
		if math.IsNaN(num) || math.IsNaN(den) {
			return math.NaN()
		}
		return num / (den + epsilon)
	}
	out["rx_tx_ratio_N3"]  = safeDivide(ratN3rx, ratN3tx)
	out["rx_tx_ratio_N6"]  = safeDivide(ratN6rx, ratN6tx)
	out["drop_fraction_N3"] = safeDivide(dropsN3, pktsN3)
	out["drop_fraction_N6"] = safeDivide(dropsN6, pktsN6)

	// Rolling stats for each rate feature
	rateSlices := map[string][]float64{
		"rate_pfcp_sessions": rfSessions,
		"rate_bytes_N3_rx":   rfBytesN3rx,
		"rate_bytes_N3_tx":   rfBytesN3tx,
		"rate_bytes_N6_rx":   rfBytesN6rx,
		"rate_bytes_N6_tx":   rfBytesN6tx,
		"rate_drops_N3":      rfDropsN3,
		"rate_drops_N6":      rfDropsN6,
	}
	for _, feat := range []string{
		"rate_pfcp_sessions",
		"rate_bytes_N3_rx", "rate_bytes_N3_tx",
		"rate_bytes_N6_rx", "rate_bytes_N6_tx",
		"rate_drops_N3", "rate_drops_N6",
	} {
		m, s, mn, mx := rollingStats(rateSlices[feat])
		out[feat+"_mean_5m"] = m
		out[feat+"_std_5m"]  = s
		out[feat+"_min_5m"]  = mn
		out[feat+"_max_5m"]  = mx
	}

	return out
}

// ApplyScaler applies StandardScaler: x_scaled = (x - mean) / scale.
// NaN inputs are left as NaN for the sidecar to impute.
func ApplyScaler(features map[string]float64, params ScalerParams) []float64 {
	vec := make([]float64, len(params.FeatureNames))
	for i, name := range params.FeatureNames {
		v, ok := features[name]
		if !ok || math.IsNaN(v) {
			vec[i] = math.NaN()
			continue
		}
		if params.Scale[i] < epsilon {
			vec[i] = 0
			continue
		}
		vec[i] = (v - params.Mean[i]) / params.Scale[i]
	}
	return vec
}
