package tier2

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"sync"
	"time"

	"bess.internal/upf-detector/internal/store"
	"bess.internal/upf-detector/internal/vmclient"
)

const (
	// aiEventTypeML is the event_type value for AI (MOMENT) anomaly events.
	aiEventTypeML = "ml"

	// syntheticMLMetric is the metric_name used for AI events (multivariate).
	syntheticMLMetric = "upf_multivariate_ai"

	// aiDedupWindow: suppress duplicate AI ML events per fingerprint within this window.
	aiDedupWindow = 5 * time.Minute

	// momentWindowSize: MOMENT context window in steps.
	momentWindowSize = 512

	// chronosStaleSteps: if uoi_value has not updated in this many steps, skip Chronos.
	chronosStaleSteps = 7 // 7 × 15s = 105s > 90s UOI cadence + 1 step margin

	// channelCount: number of MOMENT channels (matches train_moment.py MOMENT_CHANNELS order).
	channelCount = 16

	// analysisRCATimeout: max time to wait for Brain 2 RCA response.
	analysisRCATimeout = 10 * time.Second
)

// MOMENT channel names in the same order as MOMENT_CHANNELS in train_moment.py.
var momentChannelOrder = []string{
	"port_bytes_N3_rx_rate",
	"port_bytes_N6_tx_rate",
	"port_pkts_N3_rx_rate",
	"port_dropped_N3_rx_rate",
	"port_dropped_N6_rx_rate",
	"pfcp_sessions_total",
	"pfcp_session_setup_rate",
	"dl_forwarding_efficiency",
	"dl_throughput_efficiency",
	"drop_rate_percentage",
	"tsi_value",
	"uoi_session_component",
	"uoi_throughput_component",
	"go_goroutines",
	"go_heap_alloc_bytes",
	"gc_pressure_rate",
}

// circularBuffer is a ring buffer of float64 for a single channel.
type circularBuffer struct {
	mu   sync.Mutex
	data []float64
	size int
	head int    // next write index
	full bool   // true after first complete fill
}

func newCircularBuffer(size int) *circularBuffer {
	return &circularBuffer{
		data: make([]float64, size),
		size: size,
	}
}

// push appends a value. Returns true when the buffer is full (ready for inference).
func (c *circularBuffer) push(v float64) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.data[c.head] = v
	c.head = (c.head + 1) % c.size
	if c.head == 0 {
		c.full = true
	}
	return c.full
}

// snapshot returns a copy of the buffer contents in temporal order (oldest first).
// Returns nil if not yet full.
func (c *circularBuffer) snapshot() []float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.full {
		return nil
	}
	out := make([]float64, c.size)
	copy(out[:c.size-c.head], c.data[c.head:])
	copy(out[c.size-c.head:], c.data[:c.head])
	return out
}

// staleCount returns how many trailing steps have the same value (staleness indicator).
func (c *circularBuffer) staleCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.full {
		return 0
	}
	// Check the last chronosStaleSteps values
	check := min(c.size, chronosStaleSteps+1)
	last := c.data[(c.head-1+c.size)%c.size]
	count := 0
	for i := 1; i < check; i++ {
		idx := (c.head - 1 - i + c.size) % c.size
		if math.Abs(c.data[idx]-last) < 1e-9 {
			count++
		} else {
			break
		}
	}
	return count
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// ----- HTTP request/response types for sidecars ---------------------------------

type momentDetectRequest struct {
	Channels     [][]float64 `json:"channels"`      // n_channels × seq_len
	ChannelNames []string    `json:"channel_names"` // ordered channel names
}

type momentDetectResponse struct {
	Available           bool               `json:"available"`
	Anomaly             bool               `json:"anomaly"`
	AnomalyScore        float64            `json:"anomaly_score"`
	Threshold           float64            `json:"threshold"`
	ChannelScores       map[string]float64 `json:"channel_scores"`
	TopAnomalousChannels []string          `json:"top_anomalous_channels"`
	Confidence          float64            `json:"confidence"`
	ModelVersion        string             `json:"model_version"`
}

type chronosForecastRequest struct {
	Context    []float64 `json:"context"`
	Timestamps []string  `json:"timestamps,omitempty"`
}

type chronosForecastResponse struct {
	Available              bool      `json:"available"`
	ForecastMedian         []float64 `json:"forecast_median"`
	ForecastP10            []float64 `json:"forecast_p10"`
	ForecastP90            []float64 `json:"forecast_p90"`
	ForecastTimestamps     []string  `json:"forecast_timestamps"`
	CapacityBreachEtaMinutes *float64 `json:"capacity_breach_eta_minutes"`
	BreachProbability      float64   `json:"breach_probability"`
	Trend                  string    `json:"trend"`
	ModelVersion           string    `json:"model_version"`
}

// analyzeAnomalyRequest is sent to the analysis service for RCA.
type analyzeAnomalyRequest struct {
	MetricName          string             `json:"metric_name"`
	LabelsJSON          string             `json:"labels"`
	AnomalyScore        float64            `json:"anomaly_score"`
	Threshold           float64            `json:"threshold"`
	TopChannels         []string           `json:"top_channels"`
	ChannelScores       map[string]float64 `json:"channel_scores"`
	BreachProbability   *float64           `json:"breach_probability,omitempty"`
	BreachEtaMinutes    *float64           `json:"breach_eta_minutes,omitempty"`
	Trend               string             `json:"trend,omitempty"`
	WindowStartISO      string             `json:"window_start_iso"`
	WindowEndISO        string             `json:"window_end_iso"`
	ModelVersions       AIModelVersions    `json:"model_versions"`
}

// analyzeAnomalyResponse is returned by the analysis service.
type analyzeAnomalyResponse struct {
	RCAReport RCAReport `json:"rca_report"`
}

// --------- AIEvaluator ----------------------------------------------------------

// AIEvaluator maintains circular buffers for all MOMENT channels + uoi_value,
// and runs MOMENT + Chronos inference at each poll cycle.
type AIEvaluator struct {
	vm          *vmclient.Client
	db          *store.Store
	momentURL   string // e.g. http://moment-sidecar:8083
	chronosURL  string // e.g. http://chronos-sidecar:8084
	analysisURL string // e.g. http://analysis:8082

	httpClient *http.Client
	log        *slog.Logger

	// Per-channel circular buffers (indexed by momentChannelOrder)
	channelBuffers []*circularBuffer
	// Univariate uoi_value buffer for Chronos
	uoiBuffer *circularBuffer
	// Timestamp buffer (parallel to channel buffers)
	tsBuf []time.Time
	tsMu  sync.Mutex
	tsIdx int

	modelVersions AIModelVersions

	// Deduplication: key = fingerprint string, value = last event time
	dedupMu    sync.Mutex
	dedupCache map[string]time.Time
}

// NewAIEvaluator creates an AIEvaluator.
// Returns nil, nil if both sidecars are unreachable on first health check.
func NewAIEvaluator(
	vm *vmclient.Client,
	db *store.Store,
	momentURL, chronosURL, analysisURL string,
	log *slog.Logger,
) (*AIEvaluator, error) {
	buffers := make([]*circularBuffer, channelCount)
	for i := range buffers {
		buffers[i] = newCircularBuffer(momentWindowSize)
	}

	ev := &AIEvaluator{
		vm:             vm,
		db:             db,
		momentURL:      momentURL,
		chronosURL:     chronosURL,
		analysisURL:    analysisURL,
		httpClient:     &http.Client{Timeout: 8 * time.Second},
		log:            log,
		channelBuffers: buffers,
		uoiBuffer:      newCircularBuffer(momentWindowSize),
		tsBuf:          make([]time.Time, momentWindowSize),
		dedupCache:     make(map[string]time.Time),
	}

	// Warm-up log — buffers need momentWindowSize steps before first inference
	log.Info("tier-2 AI evaluator initialised",
		"moment_url", momentURL,
		"chronos_url", chronosURL,
		"warm_up_steps", momentWindowSize,
		"warm_up_duration", time.Duration(momentWindowSize*15)*time.Second,
	)
	return ev, nil
}

// Push appends one scrape cycle's worth of data into the circular buffers.
// It is called by the detection main loop once per poll cycle, before Run.
// Returns how many more pushes are needed before buffers are full.
func (a *AIEvaluator) Push(ctx context.Context, ts time.Time) (remaining int) {
	now := ts
	step := 15 * time.Second
	lookback := time.Duration(2) * step // just 2 steps — get latest value

	// ----- channel data from VM -----------------------------------------------
	type channelQuery struct {
		idx   int
		query string
		// post-process: "rate" → diff/dt, "gauge" → last value
		kind string
	}

	// Map channel index → PromQL + kind
	queries := []channelQuery{
		{0, `port_bytes_count{dir="rx",iface="N3",job="upf"}`, "rate"},
		{1, `port_bytes_count{dir="tx",iface="N6",job="upf"}`, "rate"},
		{2, `port_packets_count{dir="rx",iface="N3",job="upf"}`, "rate"},
		{3, `port_dropped_count{dir="rx",iface="N3",job="upf"}`, "rate"},
		{4, `port_dropped_count{dir="rx",iface="N6",job="upf"}`, "rate"},
		{5, `pfcp_sessions_total{job="upf",node_id!=""}`, "gauge"},
		{6, `pfcp_messages_total{direction="Incoming",message_type="Session Establishment Request",job="upf"}`, "rate"},
		{7, `dl_forwarding_efficiency`, "gauge"},
		{8, `dl_throughput_efficiency_packet`, "gauge"},
		{9, `drop_rate_percentage`, "gauge"},
		{10, `tsi_value`, "gauge"},
		{11, `uoi_session_component`, "gauge"},
		{12, `uoi_throughput_component`, "gauge"},
		{13, `go_goroutines{job="upf"}`, "gauge"},
		{14, `go_memstats_heap_alloc_bytes{job="upf"}`, "gauge"},
		{15, `go_gc_duration_seconds_count{job="upf"}`, "rate"},
	}

	start := now.Add(-lookback)

	for _, cq := range queries {
		series, err := a.vm.QueryRange(ctx, cq.query, start, now, step)
		if err != nil || len(series) == 0 {
			a.channelBuffers[cq.idx].push(0.0)
			continue
		}
		// Use first series (usually one series per query)
		s := series[0]
		var val float64
		if len(s.Values) >= 2 && cq.kind == "rate" {
			dt := step.Seconds()
			delta := s.Values[len(s.Values)-1] - s.Values[len(s.Values)-2]
			if delta >= 0 && dt > 0 {
				val = delta / dt
			}
		} else if len(s.Values) >= 1 {
			val = s.Values[len(s.Values)-1]
		}
		a.channelBuffers[cq.idx].push(val)
	}

	// ----- uoi_value for Chronos -----------------------------------------------
	uoiSeries, err := a.vm.QueryRange(ctx, `uoi_value`, start, now, step)
	uoiVal := 0.0
	if err == nil && len(uoiSeries) > 0 && len(uoiSeries[0].Values) > 0 {
		uoiVal = uoiSeries[0].Values[len(uoiSeries[0].Values)-1]
	}
	a.uoiBuffer.push(uoiVal)

	// ----- timestamp buffer ----------------------------------------------------
	a.tsMu.Lock()
	a.tsBuf[a.tsIdx%momentWindowSize] = ts
	a.tsIdx++
	a.tsMu.Unlock()

	// Count remaining warm-up steps
	if !a.channelBuffers[0].full {
		written := a.tsIdx
		if written < momentWindowSize {
			return momentWindowSize - written
		}
	}
	return 0
}

// Run performs one AI Tier 2 evaluation cycle.
// Should be called after Push returns 0 (buffers full).
// Errors are logged and not propagated — Tier 2 AI is degraded-graceful.
func (a *AIEvaluator) Run(ctx context.Context, now time.Time) {
	// Build MOMENT window from channel buffers
	channels := make([][]float64, channelCount)
	ready := true
	for i, buf := range a.channelBuffers {
		snap := buf.snapshot()
		if snap == nil {
			ready = false
			break
		}
		channels[i] = snap
	}
	if !ready {
		a.log.Debug("tier-2 AI: buffers not yet full — skipping")
		return
	}

	// ----- MOMENT anomaly detection -------------------------------------------
	momentResp, err := a.callMOMENT(ctx, channels)
	if err != nil {
		a.log.Warn("tier-2 AI: MOMENT sidecar error — skipping", "err", err)
		return
	}
	if !momentResp.Available {
		a.log.Debug("tier-2 AI: MOMENT reports models not loaded")
		return
	}
	if !momentResp.Anomaly {
		return // no anomaly — nothing to do
	}

	a.log.Info("tier-2 AI: MOMENT anomaly detected",
		"score", fmt.Sprintf("%.4f", momentResp.AnomalyScore),
		"threshold", fmt.Sprintf("%.4f", momentResp.Threshold),
		"top_channels", momentResp.TopAnomalousChannels,
	)

	// ----- Deduplication by top-channel fingerprint ---------------------------
	fingerprint := channelFingerprint(momentResp.TopAnomalousChannels)
	a.dedupMu.Lock()
	lastFired, seen := a.dedupCache[fingerprint]
	if seen && time.Since(lastFired) < aiDedupWindow {
		a.dedupMu.Unlock()
		a.log.Debug("tier-2 AI: dedup suppressed duplicate event", "fingerprint", fingerprint)
		return
	}
	a.dedupCache[fingerprint] = now
	a.dedupMu.Unlock()

	// ----- Chronos-2 forecast (optional) --------------------------------------
	var chronosResp *chronosForecastResponse

	uoiSnap := a.uoiBuffer.snapshot()
	uoiStale := a.uoiBuffer.staleCount() >= chronosStaleSteps
	if uoiStale {
		a.log.Warn("tier-2 AI: uoi_value stale — skipping Chronos forecast",
			"stale_steps", a.uoiBuffer.staleCount())
	} else if uoiSnap != nil {
		chronosResp, err = a.callChronos(ctx, uoiSnap, now)
		if err != nil {
			a.log.Warn("tier-2 AI: Chronos sidecar error — continuing without forecast", "err", err)
		}
	}

	// ----- Build AITier2Event -------------------------------------------------
	windowStart := now.Add(-time.Duration(momentWindowSize*15) * time.Second)
	aiEvent := AITier2Event{
		MetricName:     syntheticMLMetric,
		LabelsJSON:     `{}`,
		AnomalyScore:   momentResp.AnomalyScore,
		Threshold:      momentResp.Threshold,
		TopChannels:    momentResp.TopAnomalousChannels,
		ChannelScores:  momentResp.ChannelScores,
		WindowStartISO: windowStart.UTC().Format(time.RFC3339),
		WindowEndISO:   now.UTC().Format(time.RFC3339),
		ModelVersions: AIModelVersions{
			MOMENTVersion: momentResp.ModelVersion,
		},
	}
	if chronosResp != nil && chronosResp.Available {
		aiEvent.BreachProbability = &chronosResp.BreachProbability
		aiEvent.BreachEtaMinutes  = chronosResp.CapacityBreachEtaMinutes
		aiEvent.ForecastMedian    = chronosResp.ForecastMedian
		aiEvent.ForecastP10       = chronosResp.ForecastP10
		aiEvent.ForecastP90       = chronosResp.ForecastP90
		aiEvent.ForecastTimestamps = chronosResp.ForecastTimestamps
		aiEvent.Trend              = chronosResp.Trend
		aiEvent.ModelVersions.ChronosVersion = chronosResp.ModelVersion
	}

	// ----- Store event in SQLite ----------------------------------------------
	confidence := momentResp.Confidence
	severity := aiSeverity(momentResp.AnomalyScore, momentResp.Threshold, aiEvent.BreachProbability)
	channelScoresJSON, _ := json.Marshal(momentResp.ChannelScores)

	ev := store.Event{
		MetricName:           syntheticMLMetric,
		Labels:               `{}`,
		Timestamp:            now,
		ObservedValue:        momentResp.AnomalyScore,
		DeviationMagnitude:   momentResp.AnomalyScore - momentResp.Threshold,
		RuleName:             "moment_reconstruction",
		Severity:             severity,
		EventType:            aiEventTypeML,
		Confidence:           &confidence,
		FeatureContributions: string(channelScoresJSON),
	}
	if err := a.db.Insert(ctx, ev); err != nil {
		a.log.Error("tier-2 AI: failed to store event", "err", err)
		return
	}

	// ----- Async RCA via Brain 2 (fire and forget with short-lived goroutine) -
	go func(event AITier2Event) {
		rcaCtx, cancel := context.WithTimeout(context.Background(), analysisRCATimeout)
		defer cancel()
		rcaJSON, err := a.callBrain2RCA(rcaCtx, event)
		if err != nil {
			a.log.Warn("tier-2 AI: Brain 2 RCA failed (non-fatal)", "err", err)
			return
		}
		if err := a.db.UpdateRCAReport(context.Background(), event.MetricName, event.LabelsJSON, rcaJSON); err != nil {
			a.log.Warn("tier-2 AI: failed to update RCA report in SQLite", "err", err)
		} else {
			a.log.Info("tier-2 AI: RCA report stored successfully")
		}
	}(aiEvent)
}

// ----- HTTP helpers -----------------------------------------------------------

func (a *AIEvaluator) callMOMENT(ctx context.Context, channels [][]float64) (*momentDetectResponse, error) {
	req := momentDetectRequest{
		Channels:     channels,
		ChannelNames: momentChannelOrder,
	}
	body, _ := json.Marshal(req)

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost,
		a.momentURL+"/detect", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := a.httpClient.Do(httpReq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("MOMENT sidecar HTTP %d", resp.StatusCode)
	}

	var r momentDetectResponse
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, fmt.Errorf("decode MOMENT response: %w", err)
	}
	return &r, nil
}

func (a *AIEvaluator) callChronos(ctx context.Context, uoiContext []float64, now time.Time) (*chronosForecastResponse, error) {
	// Build timestamp strings for the context window
	tss := make([]string, len(uoiContext))
	step := 15 * time.Second
	contextStart := now.Add(-time.Duration(len(uoiContext)) * step)
	for i := range tss {
		tss[i] = contextStart.Add(time.Duration(i) * step).UTC().Format(time.RFC3339)
	}

	req := chronosForecastRequest{
		Context:    uoiContext,
		Timestamps: tss,
	}
	body, _ := json.Marshal(req)

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost,
		a.chronosURL+"/forecast", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := a.httpClient.Do(httpReq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Chronos sidecar HTTP %d", resp.StatusCode)
	}

	var r chronosForecastResponse
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, fmt.Errorf("decode Chronos response: %w", err)
	}
	return &r, nil
}

func (a *AIEvaluator) callBrain2RCA(ctx context.Context, event AITier2Event) (string, error) {
	req := analyzeAnomalyRequest{
		MetricName:    event.MetricName,
		LabelsJSON:    event.LabelsJSON,
		AnomalyScore:  event.AnomalyScore,
		Threshold:     event.Threshold,
		TopChannels:   event.TopChannels,
		ChannelScores: event.ChannelScores,
		BreachProbability: event.BreachProbability,
		BreachEtaMinutes:  event.BreachEtaMinutes,
		Trend:             event.Trend,
		WindowStartISO:    event.WindowStartISO,
		WindowEndISO:      event.WindowEndISO,
		ModelVersions:     event.ModelVersions,
	}
	body, _ := json.Marshal(req)

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost,
		a.analysisURL+"/internal/analyze_anomaly", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := (&http.Client{Timeout: analysisRCATimeout}).Do(httpReq)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("Brain 2 /analyze_anomaly HTTP %d", resp.StatusCode)
	}

	var r analyzeAnomalyResponse
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return "", fmt.Errorf("decode RCA response: %w", err)
	}
	raw, err := json.Marshal(r.RCAReport)
	return string(raw), err
}

// ----- Helpers ----------------------------------------------------------------

// channelFingerprint builds a dedup key from the top anomalous channel names.
func channelFingerprint(channels []string) string {
	key := ""
	for i, ch := range channels {
		if i > 0 {
			key += "|"
		}
		key += ch
	}
	return key
}

// aiSeverity maps MOMENT anomaly score + Chronos breach probability to severity.
func aiSeverity(score, threshold float64, breachProb *float64) string {
	ratio := score / (threshold + 1e-9)
	bp := 0.0
	if breachProb != nil {
		bp = *breachProb
	}
	switch {
	case ratio > 5.0 || bp > 0.85:
		return "critical"
	case ratio > 3.0 || bp > 0.60:
		return "high"
	case ratio > 1.5 || bp > 0.40:
		return "medium"
	default:
		return "low"
	}
}
