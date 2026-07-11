package llm

// Eval tests verify the vocabulary contract between the LLM layer and the
// reactive/ml event distinction. These tests do NOT require a live LLM —
// they assert static properties of the prompt and tool definitions.
//
// There is no predictive/forecast event source in the V2 (ClickHouse) anomaly
// pipeline — anomaly_events only carries reactive (statistical) and ml
// (isolation forest / MOMENT) detections. Forecast uncertainty comes from a
// separate source (/api/v1/intervals, the Chronos-2 service), not from
// anomaly events, so there is deliberately no get_predictions tool.

import (
	"strings"
	"testing"
)

// TestSystemPromptReactiveMLVocabulary verifies that the system prompt
// instructs the model to use distinct language for reactive vs ml events,
// and does not claim a forecasting capability that doesn't exist.
func TestSystemPromptReactiveMLVocabulary(t *testing.T) {
	markers := []string{
		"REACTIVE",
		`event_type="reactive"`,
		`event_type="ml"`,
		`"is elevated"`,
		`"has spiked"`,
	}
	for _, m := range markers {
		if !strings.Contains(systemPrompt, m) {
			t.Errorf("system prompt missing vocabulary marker: %q", m)
		}
	}

	if strings.Contains(systemPrompt, "get_predictions") {
		t.Error("system prompt references get_predictions, which no longer exists")
	}
}

// TestGetAnomaliesToolDescription verifies the get_anomalies tool describes
// both event types it can actually return.
func TestGetAnomaliesToolDescription(t *testing.T) {
	var anomTool *ToolDefinition
	for _, t2 := range Tools() {
		if t2.Function.Name == ToolGetAnomalies {
			t2 := t2
			anomTool = &t2
			break
		}
	}
	if anomTool == nil {
		t.Fatal("get_anomalies tool not found in Tools()")
	}

	desc := anomTool.Function.Description
	for _, term := range []string{"reactive", "ml"} {
		if !strings.Contains(desc, term) {
			t.Errorf("get_anomalies description should mention event_type %q", term)
		}
	}
	if strings.Contains(desc, "get_predictions") {
		t.Error("get_anomalies description redirects to get_predictions, which no longer exists")
	}
}

// TestToolSetCompleteness verifies all expected tools are registered and that
// the removed get_predictions tool is not.
func TestToolSetCompleteness(t *testing.T) {
	expected := map[string]bool{
		ToolQueryClickHouse:   false,
		ToolGetAnomalies:      false,
		ToolGetMetricMetadata: false,
	}
	for _, tool := range Tools() {
		if tool.Function.Name == "get_predictions" {
			t.Error("get_predictions tool is still registered but has no backing service")
		}
		if _, ok := expected[tool.Function.Name]; ok {
			expected[tool.Function.Name] = true
		}
	}
	for name, found := range expected {
		if !found {
			t.Errorf("tool %q missing from Tools()", name)
		}
	}
}
