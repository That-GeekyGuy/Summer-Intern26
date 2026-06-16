package llm

// Eval tests verify the vocabulary contract between the LLM layer and the
// reactive/predictive event distinction. These tests do NOT require a live LLM —
// they assert static properties of the prompt and tool definitions that prevent
// the model from conflating "predicted" with "observed".

import (
	"strings"
	"testing"
)

// TestSystemPromptReactivePredictiveVocabulary verifies that the system prompt
// instructs the model to use distinct language for reactive vs predictive events.
func TestSystemPromptReactivePredictiveVocabulary(t *testing.T) {
	// Reactive vocabulary must be present
	reactiveMarkers := []string{
		"REACTIVE",
		"happening NOW",
		`"is elevated"`,
		`"has spiked"`,
	}
	for _, m := range reactiveMarkers {
		if !strings.Contains(systemPrompt, m) {
			t.Errorf("system prompt missing reactive vocabulary marker: %q", m)
		}
	}

	// Predictive vocabulary must be present and clearly differentiated
	predictiveMarkers := []string{
		"PREDICTIVE",
		"FUTURE",
		`"is forecast to"`,
		`"is projected to breach"`,
		"NEVER describe a predictive event",
	}
	for _, m := range predictiveMarkers {
		if !strings.Contains(systemPrompt, m) {
			t.Errorf("system prompt missing predictive vocabulary marker: %q", m)
		}
	}
}

// TestGetPredictionsToolDescription verifies that the get_predictions tool
// description explicitly prohibits present-tense language for forecast events.
func TestGetPredictionsToolDescription(t *testing.T) {
	var predTool *ToolDefinition
	for _, t2 := range Tools() {
		if t2.Function.Name == ToolGetPredictions {
			t2 := t2
			predTool = &t2
			break
		}
	}
	if predTool == nil {
		t.Fatal("get_predictions tool not found in Tools()")
	}

	desc := predTool.Function.Description
	futureTerms := []string{"forecast", "projected", "FUTURE", "NEVER"}
	for _, term := range futureTerms {
		if !strings.Contains(desc, term) {
			t.Errorf("get_predictions description missing future-tense guard %q", term)
		}
	}

	// Must not use present-tense language that implies current observation
	forbiddenTerms := []string{"currently observed", "happening now", "currently detected"}
	for _, term := range forbiddenTerms {
		if strings.Contains(strings.ToLower(desc), strings.ToLower(term)) {
			t.Errorf("get_predictions description uses forbidden present-tense language: %q", term)
		}
	}
}

// TestGetAnomaliesToolDescription verifies the get_anomalies tool is scoped to
// reactive events and explicitly redirects forecasts to get_predictions.
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
	if !strings.Contains(desc, "REACTIVE") {
		t.Error("get_anomalies description should explicitly state it returns REACTIVE events")
	}
	if !strings.Contains(desc, "get_predictions") {
		t.Error("get_anomalies description should redirect forecast queries to get_predictions")
	}
}

// TestToolSetCompleteness verifies all expected tools are registered.
func TestToolSetCompleteness(t *testing.T) {
	expected := map[string]bool{
		ToolQueryPrometheus:   false,
		ToolGetAnomalies:      false,
		ToolGetPredictions:    false,
		ToolGetMetricMetadata: false,
	}
	for _, tool := range Tools() {
		expected[tool.Function.Name] = true
	}
	for name, found := range expected {
		if !found {
			t.Errorf("tool %q missing from Tools()", name)
		}
	}
}
