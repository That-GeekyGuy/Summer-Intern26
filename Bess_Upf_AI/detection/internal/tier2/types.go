package tier2

// AITier2Event is emitted by the AIEvaluator when MOMENT detects an anomaly.
// It is a superset of the regular Event and carries enough context for Brain 2 RCA.
type AITier2Event struct {
	// Identification
	MetricName string
	LabelsJSON string

	// MOMENT anomaly score
	AnomalyScore   float64
	Threshold      float64
	TopChannels    []string // top 5 most anomalous channels
	ChannelScores  map[string]float64

	// Chronos-2 forecast (nil if Chronos unavailable or UOI stale)
	BreachProbability    *float64
	BreachEtaMinutes     *float64
	ForecastMedian       []float64
	ForecastP10          []float64
	ForecastP90          []float64
	ForecastTimestamps   []string
	Trend                string // "increasing" | "decreasing" | "stable"

	// Context for RCA
	WindowStartISO string // ISO timestamp of window start
	WindowEndISO   string // ISO timestamp of window end (= anomaly detection time)
	ModelVersions  AIModelVersions
}

// AIModelVersions tracks which model versions fired for an event.
type AIModelVersions struct {
	MOMENTVersion  string `json:"moment_version"`
	ChronosVersion string `json:"chronos_version"`
}

// RCAReport is the structured JSON written to rca_report column after Brain 2 analysis.
// It also surfaces in the GET /anomalies response.
type RCAReport struct {
	Severity   string `json:"severity"` // "low" | "medium" | "high" | "critical"
	Cause      string `json:"cause"`    // short human-readable cause description
	Summary    string `json:"summary"`  // 2-3 sentence explanation
	Evidence   []string `json:"evidence"` // bullet-point observations from PromQL
	Recommended []string `json:"recommended_actions"`
	Confidence float64  `json:"confidence"` // 0.0–1.0, model confidence in this RCA
	ModelAttr  string   `json:"model_attribution"` // e.g. "MOMENT-1-large (linear probing)"
}
