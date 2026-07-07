package llm

import "encoding/json"

// Tool name constants used by the orchestrator for dispatch.
const (
	ToolQueryClickHouse   = "query_clickhouse"
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
				Name:        ToolQueryClickHouse,
				Description: "Execute a SQL query against ClickHouse and return rows. Tables available: bess_upf.upf_metrics (columns: ts, upf_id, pfcp_sessions_total, port_bytes_N3_rx_rate, port_dropped_N3_rx_rate, etc) and bess_upf.anomaly_events.",
				Parameters: mustJSON(map[string]any{
					"type": "object",
					"properties": map[string]any{
						"sql": map[string]any{
							"type":        "string",
							"description": "Valid ClickHouse SQL expression.",
						},
					},
					"required": []string{"sql"},
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
				Description: "List all available metric names. Use this before constructing any SQL query to avoid hallucinated metric names.",
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
