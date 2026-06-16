package detector

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

// AnomalyEvent mirrors store.QueryResult from the detection service.
type AnomalyEvent struct {
	ID                    int64     `json:"id"`
	MetricName            string    `json:"metric_name"`
	Labels                string    `json:"labels"`
	Timestamp             time.Time `json:"timestamp"`
	ObservedValue         float64   `json:"observed_value"`
	ExpectedValue         *float64  `json:"expected_value,omitempty"`
	DeviationMagnitude    float64   `json:"deviation_magnitude"`
	RuleName              string    `json:"rule_name"`
	Severity              string    `json:"severity"`
	EventType             string    `json:"event_type"`
	ForecastHorizon       string    `json:"forecast_horizon,omitempty"`
	PredictedCrossingTime *int64    `json:"predicted_crossing_time,omitempty"`
	Confidence            *float64  `json:"confidence,omitempty"`
	ThresholdConfig       string    `json:"threshold_config,omitempty"`
	CreatedAt             time.Time `json:"created_at"`
}

// Client is an HTTP client for the detection service's /anomalies endpoint.
type Client struct {
	baseURL string
	http    *http.Client
}

func New(baseURL string) *Client {
	return &Client{baseURL: baseURL, http: &http.Client{Timeout: 10 * time.Second}}
}

// GetAnomalies fetches recent anomaly events (all types) from the detection service.
func (c *Client) GetAnomalies(ctx context.Context, since time.Duration, metric, severity string) ([]AnomalyEvent, error) {
	return c.query(ctx, since, metric, severity, "")
}

// GetPredictions fetches only predictive (type="predictive") events from the detection service.
func (c *Client) GetPredictions(ctx context.Context, since time.Duration, metric string) ([]AnomalyEvent, error) {
	return c.query(ctx, since, metric, "", "predictive")
}

func (c *Client) query(ctx context.Context, since time.Duration, metric, severity, eventType string) ([]AnomalyEvent, error) {
	sinceTime := time.Now().Add(-since)
	params := url.Values{"since": {fmt.Sprintf("%d", sinceTime.Unix())}}
	if metric != "" {
		params.Set("metric", metric)
	}
	if severity != "" {
		params.Set("severity", severity)
	}
	if eventType != "" {
		params.Set("type", eventType)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		c.baseURL+"/anomalies?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("get anomalies: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("detection service HTTP %d", resp.StatusCode)
	}

	var body struct {
		Anomalies []AnomalyEvent `json:"anomalies"`
		Total     int            `json:"total"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decode anomalies response: %w", err)
	}
	return body.Anomalies, nil
}
