package llm

import "encoding/json"

// Tool name constants used by the orchestrator for dispatch.
const (
	ToolQueryPrometheus   = "query_prometheus"
	ToolGetAnomalies      = "get_anomalies"
	ToolGetPredictions    = "get_predictions"
	ToolGetMetricMetadata = "get_metric_metadata"
)

// Tools returns the OpenAI-format tool definitions to send with every request.
func Tools() []ToolDefinition {
	return []ToolDefinition{
		{
			Type: "function",
			Function: ToolFunctionDef{
				Name:        ToolQueryPrometheus,
				Description: "Execute a PromQL query against VictoriaMetrics and return summary statistics. Only metrics listed by get_metric_metadata can be queried.",
				Parameters: mustJSON(map[string]any{
					"type": "object",
					"properties": map[string]any{
						"promql": map[string]any{
							"type":        "string",
							"description": "Valid PromQL expression. Use only metric names from get_metric_metadata.",
						},
						"time_range": map[string]any{
							"type":        "string",
							"description": "Duration to look back, e.g. '1h', '30m', '24h'. Maximum 30 days.",
						},
						"step": map[string]any{
							"type":        "string",
							"description": "Resolution step, e.g. '1m', '5m'. Minimum 15s. Omit to use 1m default.",
						},
					},
					"required": []string{"promql", "time_range"},
				}),
			},
		},
		{
			Type: "function",
			Function: ToolFunctionDef{
				Name:        ToolGetAnomalies,
				Description: "Retrieve recent REACTIVE anomaly events — conditions currently observed or recently detected by statistical rules (z-score, trend deviation, threshold breach). Do NOT use this for future projections; use get_predictions for that.",
				Parameters: mustJSON(map[string]any{
					"type": "object",
					"properties": map[string]any{
						"since": map[string]any{
							"type":        "string",
							"description": "How far back to look, e.g. '1h', '24h', '7d'.",
						},
						"metric": map[string]any{
							"type":        "string",
							"description": "Optional: filter by metric name.",
						},
						"severity": map[string]any{
							"type":        "string",
							"description": "Optional: filter by severity.",
							"enum":        []string{"low", "medium", "high", "critical"},
						},
					},
					"required": []string{"since"},
				}),
			},
		},
		{
			Type: "function",
			Function: ToolFunctionDef{
				Name:        ToolGetPredictions,
				Description: "Retrieve PREDICTIVE forecast events — linear-regression projections that a metric will breach a capacity ceiling within the forecast horizon. These represent FUTURE projected states, not real-time observations. Use language like 'is forecast to', 'is projected to breach', 'trend suggests' — NEVER describe these as something seen, detected, or measured right now.",
				Parameters: mustJSON(map[string]any{
					"type": "object",
					"properties": map[string]any{
						"since": map[string]any{
							"type":        "string",
							"description": "How far back to look for forecast events, e.g. '1h', '2h'.",
						},
						"metric": map[string]any{
							"type":        "string",
							"description": "Optional: filter by metric name.",
						},
					},
					"required": []string{"since"},
				}),
			},
		},
		{
			Type: "function",
			Function: ToolFunctionDef{
				Name:        ToolGetMetricMetadata,
				Description: "List all available metric names. Use this before constructing any PromQL query to avoid hallucinated metric names.",
				Parameters:  mustJSON(map[string]any{"type": "object", "properties": map[string]any{}}),
			},
		},
	}
}

func mustJSON(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}
