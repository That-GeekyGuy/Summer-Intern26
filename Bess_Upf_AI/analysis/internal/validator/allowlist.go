package validator

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"sync"
	"time"
)

// DynamicAllowlist fetches metric names from VictoriaMetrics and caches them.
// It is safe for concurrent use. An empty cache returns false for all names
// (safe-fail: if VM is unreachable at startup, no queries are permitted).
type DynamicAllowlist struct {
	mu     sync.RWMutex
	names  map[string]struct{}
	vmURL  string
	user   string
	pass   string
	client *http.Client
}

// NewDynamicAllowlist creates an allowlist backed by VM's label-values API.
func NewDynamicAllowlist(vmURL, user, pass string) *DynamicAllowlist {
	return &DynamicAllowlist{
		names:  make(map[string]struct{}),
		vmURL:  vmURL,
		user:   user,
		pass:   pass,
		client: &http.Client{Timeout: 10 * time.Second},
	}
}

// IsAllowed reports whether name is in the cached allowlist.
// Returns false when the cache is empty (not yet populated).
func (d *DynamicAllowlist) IsAllowed(name string) bool {
	d.mu.RLock()
	defer d.mu.RUnlock()
	if len(d.names) == 0 {
		return false
	}
	_, ok := d.names[name]
	return ok
}

// Size returns the current number of allowed metric names.
func (d *DynamicAllowlist) Size() int {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return len(d.names)
}

// Names returns a sorted slice of all allowed metric names.
func (d *DynamicAllowlist) Names() []string {
	d.mu.RLock()
	defer d.mu.RUnlock()
	out := make([]string, 0, len(d.names))
	for n := range d.names {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// Refresh fetches the current __name__ label values from VictoriaMetrics.
func (d *DynamicAllowlist) Refresh(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		d.vmURL+"/api/v1/label/__name__/values", nil)
	if err != nil {
		return err
	}
	if d.user != "" {
		req.SetBasicAuth(d.user, d.pass)
	}

	resp, err := d.client.Do(req)
	if err != nil {
		return fmt.Errorf("fetch metric names: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("VM returned HTTP %d", resp.StatusCode)
	}

	var body struct {
		Status string   `json:"status"`
		Data   []string `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return fmt.Errorf("decode VM response: %w", err)
	}
	if body.Status != "success" {
		return fmt.Errorf("VM status %q", body.Status)
	}

	names := make(map[string]struct{}, len(body.Data))
	for _, n := range body.Data {
		names[n] = struct{}{}
	}
	d.mu.Lock()
	d.names = names
	d.mu.Unlock()
	return nil
}

// StartRefreshLoop runs Refresh every interval until ctx is cancelled.
// Errors are passed to logFn but do not stop the loop.
func (d *DynamicAllowlist) StartRefreshLoop(ctx context.Context, interval time.Duration, logFn func(string, ...any)) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if err := d.Refresh(ctx); err != nil {
					logFn("allowlist refresh failed", "err", err)
				}
			}
		}
	}()
}
