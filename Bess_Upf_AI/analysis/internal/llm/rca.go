package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
	"bess.internal/upf-analysis/internal/chclient"
)

// correlationEdge is one lagged correlation from models/correlation_graph.json.
type correlationEdge struct {
	From           string  `json:"from"`
	To             string  `json:"to"`
	LagSeconds     int     `json:"lag_seconds"`
	Correlation    float64 `json:"correlation"`
	AbsCorrelation float64 `json:"abs_correlation"`
	Interpretation string  `json:"interpretation"`
}

// correlationGraph is the full graph loaded from models/correlation_graph.json.
type correlationGraph struct {
	DataCoverageDays float64           `json:"data_coverage_days"`
	Edges            []correlationEdge `json:"edges"`
	Warning          string            `json:"warning"`
}

// RCAEngine generates Root Cause Analysis reports for MOMENT anomaly events.
// It uses the existing LLM client + VM query tool to produce a structured JSON report.
type RCAEngine struct {
	llm       *Client
	vm        VMQuerier
	validator PromQLValidator
	log       *slog.Logger

	// Correlation graph — loaded lazily once, then cached.
	corrOnce  sync.Once
	corrGraph *correlationGraph
}

// corrGraphPath returns the expected path of the correlation graph JSON.
// Respects MODELS_DIR env var (default: /models relative to executable's parent).
func corrGraphPath() string {
	dir := os.Getenv("MODELS_DIR")
	if dir == "" {
		exe, _ := os.Executable()
		dir = filepath.Join(filepath.Dir(exe), "..", "models")
	}
	return filepath.Join(dir, "correlation_graph.json")
}

func (r *RCAEngine) loadCorrGraph() *correlationGraph {
	r.corrOnce.Do(func() {
		path := corrGraphPath()
		data, err := os.ReadFile(path)
		if err != nil {
			r.log.Info("correlation graph not found — skipping causal context", "path", path)
			return
		}
		var g correlationGraph
		if err := json.Unmarshal(data, &g); err != nil {
			r.log.Warn("correlation graph parse failed", "err", err)
			return
		}
		r.corrGraph = &g
		r.log.Info("correlation graph loaded", "edges", len(g.Edges), "coverage_days", g.DataCoverageDays)
	})
	return r.corrGraph
}

// VMQuerier is the minimal interface of chclient.Client that RCAEngine needs.
type VMQuerier interface {
	QueryInstant(ctx context.Context, sql string) ([]chclient.InstantSample, error)
}

// PromQLValidator is the minimal interface of validator.Validator that RCAEngine needs.
type PromQLValidator interface {
	Validate(promql string, timeRange, step time.Duration) error
}

// RCARequest is the payload received from the detection service.
type RCARequest struct {
	MetricName        string             `json:"metric_name"`
	LabelsJSON        string             `json:"labels"`
	AnomalyScore      float64            `json:"anomaly_score"`
	Threshold         float64            `json:"threshold"`
	TopChannels       []string           `json:"top_channels"`
	ChannelScores     map[string]float64 `json:"channel_scores"`
	BreachProbability *float64           `json:"breach_probability,omitempty"`
	BreachEtaMinutes  *float64           `json:"breach_eta_minutes,omitempty"`
	Trend             string             `json:"trend,omitempty"`
	WindowStartISO    string             `json:"window_start_iso"`
	WindowEndISO      string             `json:"window_end_iso"`
	ModelVersions     RCAModelVersions   `json:"model_versions"`
}

// RCAModelVersions tracks model provenance.
type RCAModelVersions struct {
	MOMENTVersion  string `json:"moment_version"`
	ChronosVersion string `json:"chronos_version"`
}

// RCAReport is the structured JSON output stored in SQLite and returned to the frontend.
type RCAReport struct {
	Severity            string   `json:"severity"`
	Cause               string   `json:"cause"`
	Summary             string   `json:"summary"`
	Evidence            []string `json:"evidence"`
	RecommendedActions  []string `json:"recommended_actions"`
	Confidence          float64  `json:"confidence"`
	ModelAttribution    string   `json:"model_attribution"`
	EtaMinutes          *float64 `json:"eta_minutes,omitempty"`
}

// NewRCAEngine creates an RCAEngine.
func NewRCAEngine(llm *Client, vm VMQuerier, validator PromQLValidator, log *slog.Logger) *RCAEngine {
	return &RCAEngine{
		llm:       llm,
		vm:        vm,
		validator: validator,
		log:       log,
	}
}

// Analyze produces an RCA report for the given anomaly event.
// It queries VM for the top anomalous channels, then asks the LLM for structured analysis.
func (r *RCAEngine) Analyze(ctx context.Context, req RCARequest) (*RCAReport, error) {
	r.log.Info("RCA engine: generating report",
		"top_channels", req.TopChannels,
		"anomaly_score", fmt.Sprintf("%.4f", req.AnomalyScore),
	)

	// Gather live metric evidence for the top anomalous channels
	evidence := r.gatherEvidence(ctx, req.TopChannels, req.ChannelScores)

	// Build structured prompt
	prompt := r.buildPrompt(req, evidence)

	// Call LLM
	msgs := []Message{
		SystemMessage(string(rcaSystemPrompt)),
		UserMessage(prompt),
	}

	llmReq := ChatRequest{
		Model:       r.llm.model,
		Messages:    msgs,
		Temperature: 0.1,
		MaxTokens:   800,
	}

	body, err := json.Marshal(llmReq)
	if err != nil {
		return nil, fmt.Errorf("marshal LLM request: %w", err)
	}

	ctx2, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()

	respBody, err := r.llm.callRaw(ctx2, "/v1/chat/completions", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("LLM call: %w", err)
	}

	// Parse the LLM response — extract the JSON block from the assistant's reply
	report, err := extractRCAReport(respBody)
	if err != nil {
		r.log.Warn("RCA: failed to extract JSON from LLM response — using fallback", "err", err)
		report = r.fallbackReport(req, evidence)
	}

	// Sanity-check required fields
	report = sanitizeReport(report, req)
	r.log.Info("RCA engine: report generated", "severity", report.Severity, "cause", report.Cause)
	return report, nil
}

// gatherEvidence queries VM for the last 15 minutes of each top anomalous channel.
func (r *RCAEngine) gatherEvidence(ctx context.Context, topChannels []string, scores map[string]float64) []string {
	var evidence []string

	// Map logical channel name → PromQL template
	channelPromQL := map[string]string{
		"port_bytes_N3_rx_rate":     `rate(port_bytes_count{dir="rx",iface="N3",job="upf"}[5m])`,
		"port_bytes_N6_tx_rate":     `rate(port_bytes_count{dir="tx",iface="N6",job="upf"}[5m])`,
		"port_pkts_N3_rx_rate":      `rate(port_packets_count{dir="rx",iface="N3",job="upf"}[5m])`,
		"port_dropped_N3_rx_rate":   `rate(port_dropped_count{dir="rx",iface="N3",job="upf"}[5m])`,
		"port_dropped_N6_rx_rate":   `rate(port_dropped_count{dir="rx",iface="N6",job="upf"}[5m])`,
		"pfcp_sessions_total":       `pfcp_sessions_total{job="upf",node_id!=""}`,
		"pfcp_session_setup_rate":   `rate(pfcp_messages_total{direction="Incoming",message_type="Session Establishment Request",job="upf"}[5m])`,
		"dl_forwarding_efficiency":  `dl_forwarding_efficiency`,
		"dl_throughput_efficiency":  `dl_throughput_efficiency_packet`,
		"drop_rate_percentage":      `drop_rate_percentage`,
		"tsi_value":                 `tsi_value`,
		"uoi_session_component":     `uoi_session_component`,
		"uoi_throughput_component":  `uoi_throughput_component`,
		"go_goroutines":             `go_goroutines{job="upf"}`,
		"go_heap_alloc_bytes":       `go_memstats_heap_alloc_bytes{job="upf"}`,
		"gc_pressure_rate":          `rate(go_gc_duration_seconds_count{job="upf"}[5m])`,
	}

	channelDisplayNames := map[string]string{
		"port_bytes_N3_rx_rate":    "N3 Inbound Throughput",
		"port_bytes_N6_tx_rate":    "N6 Outbound Throughput",
		"port_pkts_N3_rx_rate":     "N3 Packet Rate",
		"port_dropped_N3_rx_rate":  "N3 Drop Rate",
		"port_dropped_N6_rx_rate":  "N6 Drop Rate",
		"pfcp_sessions_total":      "PFCP Active Sessions",
		"pfcp_session_setup_rate":  "Session Setup Rate",
		"dl_forwarding_efficiency": "DL Forwarding Efficiency",
		"dl_throughput_efficiency": "DL Throughput Efficiency",
		"drop_rate_percentage":     "Drop Rate (%)",
		"tsi_value":                "TSI Value",
		"uoi_session_component":    "UOI Session Component",
		"uoi_throughput_component": "UOI Throughput Component",
		"go_goroutines":            "Go Goroutines",
		"go_heap_alloc_bytes":      "Go Heap Allocation",
		"gc_pressure_rate":         "GC Pressure Rate",
	}

	for _, ch := range topChannels {
		q, ok := channelPromQL[ch]
		if !ok {
			continue
		}
		results, err := r.vm.QueryInstant(ctx, q)
		if err != nil || len(results) == 0 {
			continue
		}
		displayName := channelDisplayNames[ch]
		if displayName == "" {
			displayName = ch
		}
		val := results[0].Value
		score := scores[ch]
		evidence = append(evidence,
			fmt.Sprintf("%s = %.3g  (anomaly contribution: %.3f)", displayName, val, score))
	}

	// Add UOI value as context
	uoiResults, err := r.vm.QueryInstant(ctx, `uoi_value`)
	if err == nil && len(uoiResults) > 0 {
		evidence = append(evidence, fmt.Sprintf("UOI Value (current) = %.4f", uoiResults[0].Value))
	}

	return evidence
}

// formatCorrelationSection formats relevant correlation edges for the top anomalous channels.
func formatCorrelationSection(g *correlationGraph, topChannels []string) string {
	if g == nil || len(g.Edges) == 0 {
		return ""
	}

	// Find edges that connect two or more of the top anomalous channels.
	topSet := make(map[string]bool, len(topChannels))
	for _, ch := range topChannels {
		topSet[ch] = true
	}

	var relevant []correlationEdge
	for _, e := range g.Edges {
		if topSet[e.From] || topSet[e.To] {
			relevant = append(relevant, e)
		}
	}
	if len(relevant) == 0 {
		return ""
	}

	// Cap at 5 strongest edges to keep the prompt focused.
	if len(relevant) > 5 {
		relevant = relevant[:5]
	}

	var sb strings.Builder
	sb.WriteString("## Causal Chain Context (from correlation graph)\n\n")
	if g.Warning != "" {
		sb.WriteString(fmt.Sprintf("_Note: %s_\n\n", g.Warning))
	}
	for _, e := range relevant {
		sb.WriteString(fmt.Sprintf("- %s\n", e.Interpretation))
	}
	sb.WriteString("\nUse the above lagged correlations to describe the causal chain in your RCA\n")
	sb.WriteString("rather than listing metrics independently.\n\n")

	return sb.String()
}

// buildPrompt constructs the structured RCA prompt.
func (r *RCAEngine) buildPrompt(req RCARequest, evidence []string) string {
	var sb strings.Builder

	// ── Correlation graph context ─────────────────────────────────────────────
	if graph := r.loadCorrGraph(); graph != nil {
		sb.WriteString(formatCorrelationSection(graph, req.TopChannels))
	}

	sb.WriteString("## Anomaly Event Context\n\n")

	sb.WriteString(fmt.Sprintf("- **Window**: %s → %s\n", req.WindowStartISO, req.WindowEndISO))
	sb.WriteString(fmt.Sprintf("- **MOMENT Anomaly Score**: %.4f (threshold: %.4f)\n",
		req.AnomalyScore, req.Threshold))
	sb.WriteString(fmt.Sprintf("- **Score / Threshold Ratio**: %.2f×\n",
		req.AnomalyScore/(req.Threshold+1e-9)))

	if req.BreachProbability != nil {
		sb.WriteString(fmt.Sprintf("- **UOI Breach Probability**: %.1f%%\n", *req.BreachProbability*100))
	}
	if req.BreachEtaMinutes != nil {
		sb.WriteString(fmt.Sprintf("- **Breach ETA**: %.1f minutes\n", *req.BreachEtaMinutes))
	}
	if req.Trend != "" {
		sb.WriteString(fmt.Sprintf("- **UOI Trend**: %s\n", req.Trend))
	}

	if len(req.TopChannels) > 0 {
		sb.WriteString("\n### Top Anomalous Channels (ranked by MOMENT score)\n\n")
		// Sort by score descending
		type cs struct{ name string; score float64 }
		sorted := make([]cs, 0, len(req.TopChannels))
		for _, ch := range req.TopChannels {
			sorted = append(sorted, cs{ch, req.ChannelScores[ch]})
		}
		sort.Slice(sorted, func(i, j int) bool { return sorted[i].score > sorted[j].score })
		for _, s := range sorted {
			sb.WriteString(fmt.Sprintf("- %s (score: %.4f)\n", s.name, s.score))
		}
	}

	if len(evidence) > 0 {
		sb.WriteString("\n### Live Metric Evidence (instant query)\n\n")
		for _, e := range evidence {
			sb.WriteString(fmt.Sprintf("- %s\n", e))
		}
	}

	sb.WriteString("\n### Model Attribution\n\n")
	sb.WriteString(fmt.Sprintf("- MOMENT version: %s\n", req.ModelVersions.MOMENTVersion))
	sb.WriteString(fmt.Sprintf("- Chronos version: %s\n", req.ModelVersions.ChronosVersion))

	sb.WriteString(`
---

Based on the above anomaly context, generate a structured RCA report in **exactly** this JSON format:

{
  "severity": "low|medium|high|critical",
  "cause": "one-line summary of root cause (max 80 chars)",
  "summary": "2-3 sentence explanation of what happened and why",
  "evidence": ["bullet 1", "bullet 2", "bullet 3"],
  "recommended_actions": ["action 1", "action 2"],
  "confidence": 0.0-1.0,
  "eta_minutes": null,
  "model_attribution": "MOMENT-1-large reconstruction-based anomaly detection"
}

Return ONLY the JSON object. No markdown fences, no other text.`)

	return sb.String()
}

// extractRCAReport extracts the RCAReport from the LLM response body.
func extractRCAReport(body []byte) (*RCAReport, error) {
	// The LLM response is an OpenAI-format completion
	var resp struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, fmt.Errorf("parse LLM response: %w", err)
	}
	if len(resp.Choices) == 0 {
		return nil, fmt.Errorf("no choices in LLM response")
	}
	content := strings.TrimSpace(resp.Choices[0].Message.Content)

	// Strip markdown code fences if present
	content = strings.TrimPrefix(content, "```json")
	content = strings.TrimPrefix(content, "```")
	content = strings.TrimSuffix(content, "```")
	content = strings.TrimSpace(content)

	// Extract JSON object
	start := strings.Index(content, "{")
	end   := strings.LastIndex(content, "}")
	if start < 0 || end < 0 || end < start {
		return nil, fmt.Errorf("no JSON object found in LLM output")
	}
	jsonStr := content[start : end+1]

	var report RCAReport
	if err := json.Unmarshal([]byte(jsonStr), &report); err != nil {
		return nil, fmt.Errorf("parse RCA JSON: %w", err)
	}
	return &report, nil
}

// fallbackReport generates a deterministic RCA report when LLM fails.
func (r *RCAEngine) fallbackReport(req RCARequest, evidence []string) *RCAReport {
	severity := "medium"
	if req.AnomalyScore/req.Threshold > 5.0 {
		severity = "critical"
	} else if req.AnomalyScore/req.Threshold > 3.0 {
		severity = "high"
	}

	topCh := "multiple channels"
	if len(req.TopChannels) > 0 {
		topCh = req.TopChannels[0]
	}

	return &RCAReport{
		Severity: severity,
		Cause:    fmt.Sprintf("Multivariate anomaly detected — top signal: %s", topCh),
		Summary:  fmt.Sprintf("MOMENT reconstruction error (%.2f×) exceeded threshold. "+
			"Top contributing channel: %s. LLM RCA generation failed — this is a programmatic fallback.",
			req.AnomalyScore/(req.Threshold+1e-9), topCh),
		Evidence:           evidence,
		RecommendedActions: []string{"Review metrics in Grafana", "Check UPF logs for error codes"},
		Confidence:         0.5,
		ModelAttribution:   "MOMENT-1-large statistical fallback (LLM unavailable)",
	}
}

// sanitizeReport ensures required fields are present and values are in range.
func sanitizeReport(report *RCAReport, req RCARequest) *RCAReport {
	validSeverities := map[string]bool{"low": true, "medium": true, "high": true, "critical": true}
	if !validSeverities[report.Severity] {
		report.Severity = "medium"
	}
	if report.Cause == "" {
		report.Cause = "UPF multivariate anomaly"
	}
	if report.Summary == "" {
		report.Summary = fmt.Sprintf("MOMENT detected a multivariate anomaly (score=%.2f, threshold=%.2f).",
			req.AnomalyScore, req.Threshold)
	}
	if report.Confidence < 0 || report.Confidence > 1 {
		report.Confidence = 0.5
	}
	if report.ModelAttribution == "" {
		report.ModelAttribution = "MOMENT-1-large reconstruction-based anomaly detection"
	}
	// Populate eta_minutes from request if not already set by LLM
	if report.EtaMinutes == nil && req.BreachEtaMinutes != nil {
		report.EtaMinutes = req.BreachEtaMinutes
	}
	return report
}

const rcaSystemPrompt = `You are an expert 5G UPF (User Plane Function) network engineer and AI diagnostician.
You analyze multivariate anomaly detection results from MOMENT (a time-series foundation model) and produce
concise, actionable Root Cause Analysis reports.

Key domain facts:
- UPF handles 5G data plane: N3 (gNB interface) and N6 (data network interface)
- uoi_value = UPF Overload Index (normal ≈ 1.0, anomalous > 1.0, severe > 100)
- tsi_value = Traffic Spike Index
- PFCP = Packet Forwarding Control Protocol (session management)
- port_dropped_count spikes indicate buffer overflow or link saturation
- dl_forwarding_efficiency < 95% suggests packet loss on downlink
- Reconstruction error from MOMENT indicates the current joint behavior differs from learned normal patterns

Your response must be a single valid JSON object only — no markdown, no preamble, no trailing text.`
