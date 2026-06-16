package validator

import (
	"testing"
	"time"
)

// mockAllowlist is an in-memory Allowlist for tests.
type mockAllowlist struct{ allowed map[string]bool }

func newMockAllowlist(names ...string) *mockAllowlist {
	m := &mockAllowlist{allowed: make(map[string]bool)}
	for _, n := range names {
		m.allowed[n] = true
	}
	return m
}
func (m *mockAllowlist) IsAllowed(name string) bool { return m.allowed[name] }

const (
	maxRange = 30 * 24 * time.Hour
	minStep  = 15 * time.Second
)

func newV(names ...string) *Validator {
	return New(newMockAllowlist(names...), maxRange, minStep)
}

func TestValidator_ValidQuery_Passes(t *testing.T) {
	if err := newV("upf_pdu_sessions_total").Validate(
		`upf_pdu_sessions_total{job="upf"}`, time.Hour, time.Minute,
	); err != nil {
		t.Errorf("valid query rejected: %v", err)
	}
}

func TestValidator_UnknownMetric_Rejected(t *testing.T) {
	if err := newV("upf_pdu_sessions_total").Validate(
		`secret_metric{job="upf"}`, time.Hour, time.Minute,
	); err == nil {
		t.Error("expected error for unknown metric, got nil")
	}
}

func TestValidator_MalformedPromQL_Rejected(t *testing.T) {
	if err := newV().Validate(`{[invalid`, time.Hour, time.Minute); err == nil {
		t.Error("expected parse error, got nil")
	}
}

func TestValidator_ExcessiveTimeRange_Rejected(t *testing.T) {
	if err := newV("upf_pdu_sessions_total").Validate(
		`upf_pdu_sessions_total`, 31*24*time.Hour, time.Minute,
	); err == nil {
		t.Error("expected error for time range > 30d, got nil")
	}
}

func TestValidator_ExactMaxTimeRange_Passes(t *testing.T) {
	if err := newV("upf_pdu_sessions_total").Validate(
		`upf_pdu_sessions_total`, maxRange, time.Minute,
	); err != nil {
		t.Errorf("exact max range should pass: %v", err)
	}
}

func TestValidator_StepTooSmall_Rejected(t *testing.T) {
	if err := newV("upf_pdu_sessions_total").Validate(
		`upf_pdu_sessions_total`, time.Hour, time.Second,
	); err == nil {
		t.Error("expected error for step < 15s, got nil")
	}
}

func TestValidator_ExactMinStep_Passes(t *testing.T) {
	if err := newV("upf_pdu_sessions_total").Validate(
		`upf_pdu_sessions_total`, time.Hour, minStep,
	); err != nil {
		t.Errorf("exact min step should pass: %v", err)
	}
}

func TestValidator_SelectorWithoutMetricName_Rejected(t *testing.T) {
	// Fan-out selectors could match thousands of series — always block.
	if err := newV().Validate(`{job="upf"}`, time.Hour, time.Minute); err == nil {
		t.Error("expected error for selector without metric name, got nil")
	}
}

func TestValidator_RateOfAllowedMetric_Passes(t *testing.T) {
	if err := newV("upf_pdu_sessions_total").Validate(
		`rate(upf_pdu_sessions_total[5m])`, time.Hour, time.Minute,
	); err != nil {
		t.Errorf("rate() of allowed metric should pass: %v", err)
	}
}

func TestValidator_RateOfUnknownMetric_Rejected(t *testing.T) {
	if err := newV("upf_pdu_sessions_total").Validate(
		`rate(unknown_metric[5m])`, time.Hour, time.Minute,
	); err == nil {
		t.Error("expected error for rate() of unknown metric, got nil")
	}
}

func TestValidator_MultipleMetrics_AllMustBeAllowed(t *testing.T) {
	// upf_pdu_sessions_total is listed; upf_secret is not
	if err := newV("upf_pdu_sessions_total").Validate(
		`upf_pdu_sessions_total + upf_secret`, time.Hour, time.Minute,
	); err == nil {
		t.Error("expected error when any metric is not in allowlist, got nil")
	}
}

func TestValidator_EmptyAllowlist_DeniesAll(t *testing.T) {
	if err := newV().Validate(`upf_pdu_sessions_total`, time.Hour, time.Minute); err == nil {
		t.Error("expected error when allowlist is empty, got nil")
	}
}
