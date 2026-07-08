package chclient

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

type Client struct {
	BaseURL    string
	HTTPClient *http.Client
}

// InstantSample represents a single data point from CH
type InstantSample struct {
	Value float64 `json:"value"`
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

func New(baseURL string) *Client {
	return &Client{
		BaseURL: baseURL,
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
				Value: val,
				Labels: labels,
			})
		}
	}
	return samples, nil
}

// QueryRawValues for intervals/chronos
func (c *Client) QueryRawValues(ctx context.Context, sql string, lookback, step time.Duration) ([]float64, error) {
	// For simplicity in this demo, just return empty, chronos interval isn't strictly required for V2 demo
	return []float64{}, nil
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
