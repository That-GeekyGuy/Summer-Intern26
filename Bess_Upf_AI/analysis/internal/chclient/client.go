package chclient

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Client struct {
	BaseURL    string
	HTTPClient *http.Client
}

// InstantSample represents a single data point from CH
type InstantSample struct {
	Value  float64           `json:"value"`
	Labels map[string]string `json:"labels"`
}

// QueryResult represents a time series result from CH
type QueryResult struct {
	Metric map[string]string
	Values []Sample
}

type Sample struct {
	Timestamp float64
	Value     float64
}

// FeatureContribution is a single channel's weight in an anomaly's score.
type FeatureContribution struct {
	Name       string  `json:"name"`
	Importance float64 `json:"importance"`
}

// ClassifyAnomaly derives the frontend-facing event_type/severity/feature_contributions
// fields from a raw anomaly_events row. The ClickHouse schema only stores an aggregate
// anomaly_score/threshold plus the channels that tripped it, so contributions are
// weighted evenly across those channels rather than measured per-channel.
func ClassifyAnomaly(modelVersion string, score, threshold float64, channels []string) (eventType, severity, featureContributionsJSON string) {
	if strings.Contains(modelVersion, "statistical") || strings.Contains(modelVersion, "zscore") {
		eventType = "reactive"
	} else {
		eventType = "ml"
	}

	switch {
	case threshold > 0 && score > threshold*2:
		severity = "critical"
	case threshold > 0 && score > threshold*1.3:
		severity = "high"
	default:
		severity = "medium"
	}

	if eventType == "ml" && len(channels) > 0 {
		contributions := make([]FeatureContribution, len(channels))
		importance := score / float64(len(channels))
		for i, ch := range channels {
			contributions[i] = FeatureContribution{Name: ch, Importance: importance}
		}
		if b, err := json.Marshal(contributions); err == nil {
			featureContributionsJSON = string(b)
		}
	}
	return
}

// AnomalyEvent is the API-facing shape of a row from ClickHouse's anomaly_events table.
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
	FeatureContributions  string    `json:"feature_contributions,omitempty"`
	CreatedAt             time.Time `json:"created_at"`
}

func New(baseURL string) *Client {
	return &Client{
		BaseURL:    baseURL,
		HTTPClient: &http.Client{Timeout: 30 * time.Second},
	}
}

// QueryInstant runs a SQL query and extracts float values
func (c *Client) QueryInstant(ctx context.Context, sql string) ([]InstantSample, error) {
	u, err := url.Parse(c.BaseURL)
	if err != nil {
		return nil, err
	}

	reqURL := fmt.Sprintf("%s://%s/?query=%s", u.Scheme, u.Host, url.QueryEscape(sql+" FORMAT JSON"))
	if u.Path != "" && u.Path != "/" {
		reqURL += "&database=" + url.QueryEscape(u.Path[1:])
	}

	req, err := http.NewRequestWithContext(ctx, "GET", reqURL, nil)
	if err != nil {
		return nil, err
	}

	if u.User != nil {
		if pwd, ok := u.User.Password(); ok {
			req.Header.Set("X-ClickHouse-User", u.User.Username())
			req.Header.Set("X-ClickHouse-Key", pwd)
		}
	}

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("clickhouse error: %s", string(b))
	}

	var chResp struct {
		Data []map[string]any `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&chResp); err != nil {
		return nil, err
	}

	var samples []InstantSample
	for _, row := range chResp.Data {
		// look for "value" or "val" key
		v, hasValue := row["value"]
		if !hasValue {
			v, hasValue = row["val"]
		}

		if hasValue {
			var val float64
			switch v := v.(type) {
			case float64:
				val = v
			case string:
				fmt.Sscanf(v, "%f", &val)
			}

			labels := make(map[string]string)
			for k, rv := range row {
				if k != "value" && k != "val" {
					labels[k] = fmt.Sprintf("%v", rv)
				}
			}

			samples = append(samples, InstantSample{
				Value:  val,
				Labels: labels,
			})
		}
	}
	return samples, nil
}

// channelBucketQueries maps the fixed set of forecast channel keys (must match
// FORECAST_METRICS in frontend/src/features/forecast/ForecastPage.tsx) to a
// ClickHouse query template that buckets each UPF node's latest value per
// interval, then sums across nodes to get a cluster-wide time series. %d verbs
// are step-seconds then lookback-seconds, in that order.
var channelBucketQueries = map[string]string{
	"pfcp_sessions_total": `
		SELECT bucket, sum(v) AS value FROM (
			SELECT upf_id, toStartOfInterval(ts, INTERVAL %d SECOND) AS bucket, argMax(pfcp_sessions_total, ts) AS v
			FROM bess_upf.upf_metrics
			WHERE ts >= now() - INTERVAL %d SECOND
			GROUP BY upf_id, bucket
		) GROUP BY bucket ORDER BY bucket`,
	"pfcp_sessions_total_cluster": `
		SELECT bucket, sum(v) AS value FROM (
			SELECT upf_id, toStartOfInterval(ts, INTERVAL %d SECOND) AS bucket, argMax(pfcp_sessions_total, ts) AS v
			FROM bess_upf.upf_metrics
			WHERE ts >= now() - INTERVAL %d SECOND
			GROUP BY upf_id, bucket
		) GROUP BY bucket ORDER BY bucket`,
	"port_bytes_count": `
		SELECT bucket, sum(v) AS value FROM (
			SELECT upf_id, toStartOfInterval(ts, INTERVAL %d SECOND) AS bucket, argMax(port_bytes_N3_rx_rate, ts) AS v
			FROM bess_upf.upf_metrics
			WHERE ts >= now() - INTERVAL %d SECOND
			GROUP BY upf_id, bucket
		) GROUP BY bucket ORDER BY bucket`,
	"port_dropped_count": `
		SELECT bucket, sum(v1) + sum(v2) AS value FROM (
			SELECT upf_id, toStartOfInterval(ts, INTERVAL %d SECOND) AS bucket,
			       argMax(port_dropped_N3_rx_rate, ts) AS v1, argMax(port_dropped_N6_rx_rate, ts) AS v2
			FROM bess_upf.upf_metrics
			WHERE ts >= now() - INTERVAL %d SECOND
			GROUP BY upf_id, bucket
		) GROUP BY bucket ORDER BY bucket`,
}

// QueryRawValues fetches an oldest-to-newest time series for a known forecast
// channel, bucketed at `step` over the last `lookback`, for use as Chronos context.
func (c *Client) QueryRawValues(ctx context.Context, channel string, lookback, step time.Duration) ([]float64, error) {
	tmpl, ok := channelBucketQueries[channel]
	if !ok {
		return nil, fmt.Errorf("unknown forecast channel %q", channel)
	}
	sql := fmt.Sprintf(tmpl, int(step.Seconds()), int(lookback.Seconds()))

	rows, err := c.QueryJSON(ctx, sql)
	if err != nil {
		return nil, err
	}

	vals := make([]float64, 0, len(rows))
	for _, row := range rows {
		if v, ok := row["value"].(float64); ok {
			vals = append(vals, v)
		}
	}
	return vals, nil
}

// QueryJSON runs a SQL query and returns raw JSON maps.
func (c *Client) QueryJSON(ctx context.Context, sql string) ([]map[string]any, error) {
	u, err := url.Parse(c.BaseURL)
	if err != nil {
		return nil, err
	}

	reqURL := fmt.Sprintf("%s://%s/?query=%s", u.Scheme, u.Host, url.QueryEscape(sql+" FORMAT JSON"))
	if u.Path != "" && u.Path != "/" {
		reqURL += "&database=" + url.QueryEscape(u.Path[1:])
	}

	req, err := http.NewRequestWithContext(ctx, "GET", reqURL, nil)
	if err != nil {
		return nil, err
	}

	if u.User != nil {
		if pwd, ok := u.User.Password(); ok {
			req.Header.Set("X-ClickHouse-User", u.User.Username())
			req.Header.Set("X-ClickHouse-Key", pwd)
		}
	}

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("clickhouse error: %s", string(b))
	}

	var chResp struct {
		Data []map[string]any `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&chResp); err != nil {
		return nil, err
	}
	return chResp.Data, nil
}
