// Package simclient proxies scenario control requests to the upf-sim service.
package simclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Client is an HTTP client for the upf-sim /scenario endpoint.
type Client struct {
	baseURL string
	http    *http.Client
}

func New(baseURL string) *Client {
	return &Client{baseURL: baseURL, http: &http.Client{Timeout: 10 * time.Second}}
}

// ScenarioStatus mirrors the upf-sim ScenarioResponse JSON.
type ScenarioStatus struct {
	Mode             string  `json:"mode"`
	StartedAt        string  `json:"started_at"`
	UptimeSeconds    float64 `json:"uptime_seconds"`
	Duration         string  `json:"duration,omitempty"`
	RemainingSeconds float64 `json:"remaining_seconds,omitempty"`
}

// Get returns the current scenario state from the simulator.
func (c *Client) Get(ctx context.Context) (*ScenarioStatus, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/scenario", nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("upf-sim get scenario: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("upf-sim HTTP %d", resp.StatusCode)
	}
	var s ScenarioStatus
	if err := json.NewDecoder(resp.Body).Decode(&s); err != nil {
		return nil, fmt.Errorf("decode scenario: %w", err)
	}
	return &s, nil
}

// Set changes the active scenario in the simulator.
func (c *Client) Set(ctx context.Context, body io.Reader) (*ScenarioStatus, error) {
	// Buffer the body so we can forward it to the sim service.
	data, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/scenario", bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("upf-sim set scenario: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("upf-sim HTTP %d: %s", resp.StatusCode, body)
	}
	var s ScenarioStatus
	if err := json.NewDecoder(resp.Body).Decode(&s); err != nil {
		return nil, fmt.Errorf("decode scenario response: %w", err)
	}
	return &s, nil
}
