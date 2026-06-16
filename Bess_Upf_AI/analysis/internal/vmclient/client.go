package vmclient

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Client queries VictoriaMetrics for the LLM analysis backend.
type Client struct {
	baseURL  string
	username string
	password string
	http     *http.Client
}

// SeriesSummary is a compact representation of one label-set's result,
// suitable for fitting in an LLM context window.
type SeriesSummary struct {
	Labels  map[string]string `json:"labels"`
	Samples int               `json:"samples"`
	Min     float64           `json:"min"`
	Max     float64           `json:"max"`
	Avg     float64           `json:"avg"`
	Last    float64           `json:"last"`
}

// QueryResult is the structured tool result returned to the LLM.
type QueryResult struct {
	PromQL      string          `json:"promql"`
	Start       string          `json:"start"`
	End         string          `json:"end"`
	Step        string          `json:"step"`
	SeriesCount int             `json:"series_count"`
	Series      []SeriesSummary `json:"series"`
	Truncated   bool            `json:"truncated,omitempty"`
}

func New(baseURL, username, password string) *Client {
	return &Client{
		baseURL:  baseURL,
		username: username,
		password: password,
		http:     &http.Client{Timeout: 30 * time.Second},
	}
}

// InstantSample is a single scalar result from an instant query.
type InstantSample struct {
	Labels map[string]string `json:"labels"`
	Value  float64           `json:"value"`
}

// QueryInstant executes a PromQL instant query (current value) and returns
// one sample per matching time series. It is cheaper than QueryRange and
// is used by the frontend pulse strip to avoid routing through the LLM.
func (c *Client) QueryInstant(ctx context.Context, promql string) ([]InstantSample, error) {
	params := url.Values{
		"query": {promql},
		"time":  {strconv.FormatInt(time.Now().Unix(), 10)},
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		c.baseURL+"/api/v1/query?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}
	if c.username != "" {
		req.SetBasicAuth(c.username, c.password)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("query_instant: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("query_instant: HTTP %d", resp.StatusCode)
	}

	var body struct {
		Status string `json:"status"`
		Data   struct {
			Result []struct {
				Metric map[string]string `json:"metric"`
				Value  []any             `json:"value"` // [timestamp, "value_string"]
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}
	if body.Status != "success" {
		return nil, fmt.Errorf("VM status %q", body.Status)
	}

	out := make([]InstantSample, 0, len(body.Data.Result))
	for _, r := range body.Data.Result {
		if len(r.Value) != 2 {
			continue
		}
		s, ok := r.Value[1].(string)
		if !ok {
			continue
		}
		f, err := strconv.ParseFloat(s, 64)
		if err != nil {
			continue
		}
		out = append(out, InstantSample{Labels: r.Metric, Value: math.Round(f*1000) / 1000})
	}
	return out, nil
}

// QueryRange executes a PromQL range query and returns a compact summary.
// At most maxSeries series are included; the rest are silently truncated.
func (c *Client) QueryRange(ctx context.Context, promql string, timeRange, step time.Duration, maxSeries int) (*QueryResult, error) {
	end := time.Now()
	start := end.Add(-timeRange)

	params := url.Values{
		"query": {promql},
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
		return nil, fmt.Errorf("query_range: HTTP %d", resp.StatusCode)
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
		return nil, fmt.Errorf("decode response: %w", err)
	}
	if body.Status != "success" {
		return nil, fmt.Errorf("VM status %q", body.Status)
	}

	total := len(body.Data.Result)
	truncated := total > maxSeries
	if truncated {
		body.Data.Result = body.Data.Result[:maxSeries]
	}

	summaries := make([]SeriesSummary, 0, len(body.Data.Result))
	for _, r := range body.Data.Result {
		var vals []float64
		for _, v := range r.Values {
			if len(v) == 2 {
				if s, ok := v[1].(string); ok {
					if f, err := strconv.ParseFloat(s, 64); err == nil {
						vals = append(vals, f)
					}
				}
			}
		}
		if len(vals) == 0 {
			continue
		}
		mn, mx, sum := vals[0], vals[0], 0.0
		for _, v := range vals {
			if v < mn {
				mn = v
			}
			if v > mx {
				mx = v
			}
			sum += v
		}
		summaries = append(summaries, SeriesSummary{
			Labels:  r.Metric,
			Samples: len(vals),
			Min:     math.Round(mn*1000) / 1000,
			Max:     math.Round(mx*1000) / 1000,
			Avg:     math.Round(sum/float64(len(vals))*1000) / 1000,
			Last:    vals[len(vals)-1],
		})
	}

	return &QueryResult{
		PromQL:      promql,
		Start:       start.UTC().Format(time.RFC3339),
		End:         end.UTC().Format(time.RFC3339),
		Step:        step.String(),
		SeriesCount: total,
		Series:      summaries,
		Truncated:   truncated,
	}, nil
}
