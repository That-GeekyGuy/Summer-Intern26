package tier2

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"os"
	"time"

	"bess.internal/upf-detector/internal/store"
	"bess.internal/upf-detector/internal/vmclient"
)

const (
	// eventTypML is the event_type value for Tier 2 ML anomaly events.
	eventTypeML = "ml"
	// syntheticMetricName is stored in metric_name for Tier 2 events, which evaluate
	// all metrics jointly — there is no single source metric.
	syntheticMetricName = "upf_multivariate"
	// dedupWindow: suppress duplicate ML events within this window per fingerprint.
	dedupWindow = 5 * time.Minute
	// lookbackWindow: how much VM history to fetch for the feature vector.
	lookbackWindow = 5*time.Minute + 15*time.Second // one extra scrape for diff
)

// ScalerParams holds the StandardScaler parameters exported by train.py.
type ScalerParams struct {
	FeatureNames []string  `json:"feature_names"`
	Mean         []float64 `json:"mean"`
	Scale        []float64 `json:"scale"`
}

// Metadata is the subset of models/metadata.json we need at runtime.
type Metadata struct {
	Thresholds struct {
		IsolationForest float64 `json:"isolation_forest"`
		RandomForest    float64 `json:"random_forest"`
	} `json:"thresholds"`
	Warnings []string `json:"warnings"`
}

// predictRequest is the JSON body sent to POST /predict.
type predictRequest struct {
	Features map[string]float64 `json:"features"`
}

// predictResponse is the JSON body returned by the sidecar.
type predictResponse struct {
	Available            bool    `json:"available"`
	IFScore              float64 `json:"if_score"`
	RFScore              float64 `json:"rf_score"`
	RFClass              string  `json:"rf_class"`
	FeatureContributions []struct {
		Name       string  `json:"name"`
		Importance float64 `json:"importance"`
	} `json:"feature_contributions"`
}

// Evaluator runs one Tier 2 evaluation pass per poll cycle.
type Evaluator struct {
	vm          *vmclient.Client
	db          *store.Store
	scalerParams ScalerParams
	metadata    Metadata
	inferURL    string // e.g. http://ml-infer:8080
	httpClient  *http.Client
	log         *slog.Logger
}

// LoadMetadata reads models/metadata.json and models/scaler_params.json from disk.
// Returns nil, nil if the files don't exist (models not yet trained — Tier 2 unavailable).
func LoadMetadata(modelsDir string) (*Metadata, *ScalerParams, error) {
	metaPath := modelsDir + "/metadata.json"
	scalerPath := modelsDir + "/scaler_params.json"

	if _, err := os.Stat(metaPath); os.IsNotExist(err) {
		return nil, nil, nil // not yet trained
	}

	metaData, err := os.ReadFile(metaPath)
	if err != nil {
		return nil, nil, fmt.Errorf("read metadata.json: %w", err)
	}
	var meta Metadata
	if err := json.Unmarshal(metaData, &meta); err != nil {
		return nil, nil, fmt.Errorf("parse metadata.json: %w", err)
	}

	scalerData, err := os.ReadFile(scalerPath)
	if err != nil {
		return nil, nil, fmt.Errorf("read scaler_params.json: %w", err)
	}
	var sp ScalerParams
	if err := json.Unmarshal(scalerData, &sp); err != nil {
		return nil, nil, fmt.Errorf("parse scaler_params.json: %w", err)
	}

	return &meta, &sp, nil
}

// NewEvaluator creates an Evaluator. Returns nil if models are not yet trained.
func NewEvaluator(vm *vmclient.Client, db *store.Store, inferURL, modelsDir string, log *slog.Logger) (*Evaluator, error) {
	meta, sp, err := LoadMetadata(modelsDir)
	if err != nil {
		return nil, err
	}
	if meta == nil {
		log.Info("tier-2 models not found — Tier 2 unavailable (run prepare_dataset.py + train.py)")
		return nil, nil //nolint:nilnil
	}

	for _, w := range meta.Warnings {
		log.Warn("tier-2 model warning", "warning", w)
	}

	return &Evaluator{
		vm:          vm,
		db:          db,
		scalerParams: *sp,
		metadata:    *meta,
		inferURL:    inferURL,
		httpClient:  &http.Client{Timeout: 5 * time.Second},
		log:         log,
	}, nil
}

// Run performs one Tier 2 evaluation and returns any newly inserted ML events.
// Errors from the sidecar are logged and do not propagate — Tier 2 is degraded-graceful.
func (e *Evaluator) Run(ctx context.Context) ([]store.Event, error) {
	now := time.Now()
	start := now.Add(-lookbackWindow)

	// Fetch all raw series needed for feature engineering
	seriesMap, err := e.fetchRawSeries(ctx, start, now)
	if err != nil {
		e.log.Warn("tier-2 VM query failed — skipping this cycle", "err", err)
		return nil, nil
	}

	features := BuildFeatureVector(seriesMap)

	// Convert feature map to the ordered float slice for debugging / logging
	resp, err := e.callSidecar(ctx, features)
	if err != nil {
		e.log.Warn("tier-2 sidecar unavailable — skipping this cycle", "err", err)
		return nil, nil
	}
	if !resp.Available {
		e.log.Debug("tier-2 sidecar reports models not loaded")
		return nil, nil
	}

	var events []store.Event

	// Check IF threshold
	if resp.IFScore < e.metadata.Thresholds.IsolationForest {
		ev, err := e.emitEvent(ctx, "ml_isolation_forest", resp.IFScore,
			e.metadata.Thresholds.IsolationForest, resp, now)
		if err != nil {
			e.log.Error("tier-2 failed to store IF event", "err", err)
		} else if ev != nil {
			events = append(events, *ev)
		}
	}

	// Check RF threshold (probability of anomaly)
	if resp.RFScore >= e.metadata.Thresholds.RandomForest {
		ev, err := e.emitEvent(ctx, "ml_random_forest", resp.RFScore,
			e.metadata.Thresholds.RandomForest, resp, now)
		if err != nil {
			e.log.Error("tier-2 failed to store RF event", "err", err)
		} else if ev != nil {
			events = append(events, *ev)
		}
	}

	return events, nil
}

func (e *Evaluator) emitEvent(
	ctx context.Context,
	ruleName string,
	score, threshold float64,
	resp *predictResponse,
	now time.Time,
) (*store.Event, error) {
	labelsJSON := `{}`

	// 5-minute dedup: suppress if we already fired this rule recently
	if e.db.HasRecentEvent(ctx, syntheticMetricName, labelsJSON, eventTypeML, dedupWindow) {
		e.log.Debug("tier-2 suppressing duplicate event", "rule", ruleName)
		return nil, nil
	}

	confidence := resp.RFScore
	severity := mlSeverity(confidence)

	featContribJSON, _ := json.Marshal(resp.FeatureContributions)

	ev := store.Event{
		MetricName:           syntheticMetricName,
		Labels:               labelsJSON,
		Timestamp:            now,
		ObservedValue:        score,
		DeviationMagnitude:   math.Abs(score - threshold),
		RuleName:             ruleName,
		Severity:             severity,
		EventType:            eventTypeML,
		Confidence:           &confidence,
		FeatureContributions: string(featContribJSON),
	}

	if err := e.db.Insert(ctx, ev); err != nil {
		return nil, err
	}

	e.log.Info("tier-2 anomaly event",
		"rule", ruleName,
		"score", fmt.Sprintf("%.4f", score),
		"rf_class", resp.RFClass,
		"severity", severity,
	)
	return &ev, nil
}

// fetchRawSeries queries VictoriaMetrics for the last 5 minutes of each raw metric.
// Returns a map keyed by logical name (e.g. "bytes_rx_N3") → []float64 (oldest first).
func (e *Evaluator) fetchRawSeries(ctx context.Context, start, end time.Time) (map[string][]float64, error) {
	step := 15 * time.Second
	out := make(map[string][]float64)

	// pfcp_sessions_total (gauge, one series per node)
	sess, err := e.vm.QueryRange(ctx, `pfcp_sessions_total{job="upf"}`, start, end, step)
	if err != nil {
		return nil, fmt.Errorf("pfcp_sessions_total: %w", err)
	}
	if s, ok := filterByLabels(sess, nil); ok {
		out["pfcp_sessions"] = s.Values
	}

	// port_bytes_count by (dir, iface)
	bBytes, err := e.vm.QueryRange(ctx, `port_bytes_count{job="upf"}`, start, end, step)
	if err != nil {
		return nil, fmt.Errorf("port_bytes_count: %w", err)
	}
	for _, combo := range [][2]string{{"rx", "N3"}, {"tx", "N3"}, {"rx", "N6"}, {"tx", "N6"}} {
		dir, iface := combo[0], combo[1]
		if s, ok := filterByLabels(bBytes, map[string]string{"dir": dir, "iface": iface}); ok {
			out["bytes_"+dir+"_"+iface] = s.Values
		}
	}

	// port_packets_count (rx only, for drop_fraction denominator)
	bPkts, err := e.vm.QueryRange(ctx, `port_packets_count{job="upf"}`, start, end, step)
	if err != nil {
		return nil, fmt.Errorf("port_packets_count: %w", err)
	}
	for _, iface := range []string{"N3", "N6"} {
		if s, ok := filterByLabels(bPkts, map[string]string{"dir": "rx", "iface": iface}); ok {
			out["pkts_rx_"+iface] = s.Values
		}
	}

	// port_dropped_count (rx only)
	bDrops, err := e.vm.QueryRange(ctx, `port_dropped_count{job="upf"}`, start, end, step)
	if err != nil {
		return nil, fmt.Errorf("port_dropped_count: %w", err)
	}
	for _, iface := range []string{"N3", "N6"} {
		if s, ok := filterByLabels(bDrops, map[string]string{"dir": "rx", "iface": iface}); ok {
			out["drops_rx_"+iface] = s.Values
		}
	}

	return out, nil
}

func (e *Evaluator) callSidecar(ctx context.Context, features map[string]float64) (*predictResponse, error) {
	body, err := json.Marshal(predictRequest{Features: features})
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.inferURL+"/predict", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := e.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("sidecar HTTP %d", resp.StatusCode)
	}

	var pr predictResponse
	if err := json.NewDecoder(resp.Body).Decode(&pr); err != nil {
		return nil, fmt.Errorf("decode sidecar response: %w", err)
	}
	return &pr, nil
}

// mlSeverity maps the RF anomaly probability to a severity string.
func mlSeverity(confidence float64) string {
	switch {
	case confidence >= 0.90:
		return "high"
	case confidence >= 0.70:
		return "medium"
	default:
		return "low"
	}
}
