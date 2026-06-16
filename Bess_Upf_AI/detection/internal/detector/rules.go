package detector

import "time"

const (
	SeverityLow      = "low"
	SeverityMedium   = "medium"
	SeverityHigh     = "high"
	SeverityCritical = "critical"
)

// AnomalyEvent is emitted by a Rule when an anomaly is detected.
type AnomalyEvent struct {
	MetricName         string
	Labels             map[string]string
	Timestamp          time.Time
	ObservedValue      float64
	ExpectedValue      *float64
	DeviationMagnitude float64
	RuleName           string
	Severity           string
}

// Rule is the interface implemented by each detection algorithm.
type Rule interface {
	Name() string
	// Eval runs the rule over a single time series.
	// values[i] and timestamps[i] (Unix milliseconds) are guaranteed to be the same length.
	Eval(values []float64, timestamps []int64) ([]AnomalyEvent, error)
}

// MetricConfig holds per-metric rule configurations loaded from rules.yml.
type MetricConfig struct {
	Name     string  `yaml:"name"`
	Selector string  `yaml:"selector"` // e.g. {job="upf"}
	Rules    RuleSet `yaml:"rules"`
}

// RuleSet holds optional configuration for each supported rule type.
type RuleSet struct {
	ZScore    *ZScoreConfig    `yaml:"zscore"`
	Trend     *TrendConfig     `yaml:"trend"`
	Threshold *ThresholdConfig `yaml:"threshold"`
}

// ZScoreConfig configures the rolling z-score anomaly rule.
type ZScoreConfig struct {
	Enabled   bool    `yaml:"enabled"`
	Threshold float64 `yaml:"threshold"` // default 3.0
	Severity  string  `yaml:"severity"`
}

// TrendConfig configures the linear-trend deviation rule.
type TrendConfig struct {
	Enabled            bool    `yaml:"enabled"`
	DeviationThreshold float64 `yaml:"deviation_threshold"` // relative, default 0.25
	Severity           string  `yaml:"severity"`
}

// ThresholdConfig configures static min/max bounds.
type ThresholdConfig struct {
	Min      *float64 `yaml:"min"`
	Max      *float64 `yaml:"max"`
	Severity string   `yaml:"severity"`
}

// Config is the top-level structure parsed from rules.yml.
type Config struct {
	PollingInterval string          `yaml:"polling_interval"`
	Window          string          `yaml:"window"`
	Metrics         []MetricConfig  `yaml:"metrics"`
	Forecast        *ForecastConfig `yaml:"forecast"`
}

// ForecastConfig holds Tier 3 predictive forecasting configuration.
type ForecastConfig struct {
	Enabled bool             `yaml:"enabled"`
	Window  string           `yaml:"window"`   // OLS lookback window, e.g. "30m"
	Horizon string           `yaml:"horizon"`  // prediction horizon, e.g. "1h"
	Targets []ForecastTarget `yaml:"targets"`
}

// ForecastTarget describes one metric to forecast against a capacity ceiling.
type ForecastTarget struct {
	Metric    string  `yaml:"metric"`     // display / storage name
	PromQL    string  `yaml:"promql"`     // query to execute; overrides metric+{job="upf"} default
	Capacity  float64 `yaml:"capacity"`   // upper bound; 0 means use accel_only check
	AccelOnly bool    `yaml:"accel_only"` // fire if slope is positive, regardless of threshold
	Severity  string  `yaml:"severity"`
}
