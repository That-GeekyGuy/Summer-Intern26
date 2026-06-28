package validator

// PromQL allowlist fuzz corpus — P3 security hardening.
//
// Seed vectors cover the known PromQL injection surface identified during
// the P3 security review:
//   1. Wildcard metric enumeration via {__name__=~".*"}
//   2. Range exceeding policy limit (> 720h)
//   3. Step below minimum (< 15s)
//   4. Non-allowlisted metric name
//   5. Nested subquery with sub-step below minimum
//   6. Degenerate inputs (empty, null bytes)
//
// Run seeds: go test ./internal/validator/ -run TestValidatorSeeds -v
// Run fuzzer: go test ./internal/validator/ -fuzz=FuzzValidate -fuzztime=60s

import (
	"strings"
	"testing"
	"time"
)

var seedCorpus = []struct {
	name       string
	query      string
	timeRange  time.Duration
	step       time.Duration
	wantReject bool
}{
	{
		name:       "valid_allowlisted_metric",
		query:      `pfcp_sessions_total{job="upf"}`,
		timeRange:  time.Hour,
		step:       time.Minute,
		wantReject: false,
	},
	{
		name:       "wildcard_metric_enumeration",
		query:      `{__name__=~".*"}`,
		timeRange:  time.Hour,
		step:       time.Minute,
		wantReject: true,
	},
	{
		name:       "range_exceeds_policy",
		query:      `pfcp_sessions_total`,
		timeRange:  800 * 24 * time.Hour, // 800d >> 30d limit
		step:       time.Minute,
		wantReject: true,
	},
	{
		name:       "step_below_minimum",
		query:      `pfcp_sessions_total`,
		timeRange:  time.Hour,
		step:       time.Second, // 1s < 15s minimum
		wantReject: true,
	},
	{
		name:       "non_allowlisted_metric",
		query:      `secret_internal_etcd_metric`,
		timeRange:  time.Hour,
		step:       time.Minute,
		wantReject: true,
	},
	{
		name:       "subquery_cpu_amplification",
		query:      `rate(pfcp_sessions_total[5m])[10m:1s]`,
		timeRange:  time.Hour,
		step:       time.Minute,
		wantReject: true,
	},
	{
		name:       "empty_query",
		query:      ``,
		timeRange:  time.Hour,
		step:       time.Minute,
		wantReject: true,
	},
	{
		name:       "null_byte_injection",
		query:      "pfcp_sessions_total\x00{job=\"upf\"}",
		timeRange:  time.Hour,
		step:       time.Minute,
		wantReject: true,
	},
}

// TestValidatorSeeds runs every seed deterministically — always in CI.
func TestValidatorSeeds(t *testing.T) {
	v := newV("pfcp_sessions_total", "port_bytes_count")
	for _, tc := range seedCorpus {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			err := v.Validate(tc.query, tc.timeRange, tc.step)
			rejected := err != nil
			if rejected != tc.wantReject {
				t.Errorf("Validate(%q, range=%v, step=%v) rejected=%v, want=%v (err=%v)",
					tc.query, tc.timeRange, tc.step, rejected, tc.wantReject, err)
			}
		})
	}
}

// FuzzValidate exercises the validator with arbitrary PromQL strings.
// Invariant: Validate must never panic regardless of input.
//
// Durations are passed as int64 nanoseconds since the fuzzer only supports
// scalar types; they are converted to time.Duration before calling Validate.
//
// Run: go test ./internal/validator/ -fuzz=FuzzValidate -fuzztime=60s
func FuzzValidate(f *testing.F) {
	for _, tc := range seedCorpus {
		f.Add(tc.query, int64(tc.timeRange), int64(tc.step))
	}
	f.Add(`rate(port_bytes_count[5m])`, int64(time.Hour), int64(time.Minute))
	f.Add(`sum(pfcp_sessions_total)`, int64(0), int64(0))
	f.Add(strings.Repeat("a", 4096), int64(time.Hour), int64(time.Minute))

	v := newV("pfcp_sessions_total", "port_bytes_count")
	f.Fuzz(func(t *testing.T, query string, rangeNs, stepNs int64) {
		// Must never panic — only allowed outcomes are nil or error.
		_ = v.Validate(query, time.Duration(rangeNs), time.Duration(stepNs))
	})
}
