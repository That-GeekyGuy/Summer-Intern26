// Package vmclient queries VictoriaMetrics via its /api/v1/export endpoint.
// The export endpoint streams NDJSON (one JSON object per time series) with no
// sample-count ceiling, making it suitable for bulk data extraction.
package vmclient

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Series holds a single time series returned by VictoriaMetrics.
// Timestamps are Unix milliseconds; Values and Timestamps are parallel slices.
type Series struct {
	Metric     map[string]string // all labels, including "__name__"
	Values     []float64
	Timestamps []int64 // Unix milliseconds
}

// Client is a VictoriaMetrics HTTP client.
type Client struct {
	baseURL  string
	username string
	password string
	http     *http.Client
}

// New creates a Client. username and password may be empty if VM has no auth.
func New(baseURL, username, password string) *Client {
	return &Client{
		baseURL:  baseURL,
		username: username,
		password: password,
		// 10-minute timeout covers large metric cardinality at 15 s resolution × 24 h.
		http: &http.Client{Timeout: 10 * time.Minute},
	}
}

// vmExportLine is the wire format from /api/v1/export.
type vmExportLine struct {
	Metric     map[string]string `json:"metric"`
	Values     []float64         `json:"values"`
	Timestamps []int64           `json:"timestamps"`
}

// ExportMetric streams all time series for metricName between start and end
// from VictoriaMetrics. It returns one Series per unique label combination.
func (c *Client) ExportMetric(ctx context.Context, metricName string, start, end time.Time) ([]Series, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/api/v1/export", nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}

	q := url.Values{}
	q.Set("match[]", metricName)
	q.Set("start", strconv.FormatInt(start.Unix(), 10))
	q.Set("end", strconv.FormatInt(end.Unix(), 10))
	req.URL.RawQuery = q.Encode()

	if c.username != "" {
		req.SetBasicAuth(c.username, c.password)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http get: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("VictoriaMetrics returned HTTP %d for metric %q", resp.StatusCode, metricName)
	}

	// 10 MiB scanner buffer handles time series with large label sets or long
	// arrays without reallocating. Increase if you see "token too long" errors.
	const maxLineBytes = 10 * 1024 * 1024
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, maxLineBytes), maxLineBytes)

	var out []Series
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var raw vmExportLine
		if err := json.Unmarshal(line, &raw); err != nil {
			return nil, fmt.Errorf("parse VM NDJSON line: %w", err)
		}
		out = append(out, Series{
			Metric:     raw.Metric,
			Values:     raw.Values,
			Timestamps: raw.Timestamps,
		})
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan response body: %w", err)
	}

	return out, nil
}
