package vmclient

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Client queries VictoriaMetrics via its Prometheus-compatible query_range API.
type Client struct {
	baseURL  string
	username string
	password string
	http     *http.Client
}

// TimeSeries holds the result of a single label-set from a range query.
type TimeSeries struct {
	Labels     map[string]string
	Values     []float64
	Timestamps []int64 // Unix milliseconds
}

func New(baseURL, username, password string) *Client {
	return &Client{
		baseURL:  baseURL,
		username: username,
		password: password,
		http:     &http.Client{Timeout: 30 * time.Second},
	}
}

// QueryRange fetches metric data over [start, end] at the given step interval.
func (c *Client) QueryRange(ctx context.Context, query string, start, end time.Time, step time.Duration) ([]TimeSeries, error) {
	params := url.Values{
		"query": {query},
		"start": {strconv.FormatInt(start.Unix(), 10)},
		"end":   {strconv.FormatInt(end.Unix(), 10)},
		"step":  {strconv.FormatFloat(step.Seconds(), 'f', 0, 64) + "s"},
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		c.baseURL+"/api/v1/query_range?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}
	if c.username != "" {
		req.SetBasicAuth(c.username, c.password)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("query_range: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("query_range: unexpected status %d", resp.StatusCode)
	}

	var body struct {
		Status string `json:"status"`
		Data   struct {
			Result []struct {
				Metric map[string]string `json:"metric"`
				Values [][]any           `json:"values"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decode query_range response: %w", err)
	}
	if body.Status != "success" {
		return nil, fmt.Errorf("VM returned status %q", body.Status)
	}

	series := make([]TimeSeries, 0, len(body.Data.Result))
	for _, r := range body.Data.Result {
		var values []float64
		var timestamps []int64
		for _, v := range r.Values {
			if len(v) != 2 {
				continue
			}
			tsF, ok := v[0].(float64)
			if !ok {
				continue
			}
			valStr, ok := v[1].(string)
			if !ok {
				continue
			}
			val, err := strconv.ParseFloat(valStr, 64)
			if err != nil {
				continue
			}
			timestamps = append(timestamps, int64(tsF*1000))
			values = append(values, val)
		}
		series = append(series, TimeSeries{Labels: r.Metric, Values: values, Timestamps: timestamps})
	}
	return series, nil
}
