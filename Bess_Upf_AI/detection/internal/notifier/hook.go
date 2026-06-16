package notifier

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"bess.internal/upf-detector/internal/store"
)

// ActionHook receives notification calls when event state transitions occur.
//
// IMPORTANT: Implementations MUST NOT autonomously remediate infrastructure.
// This interface is for human-in-the-loop recommendations only. The dispatcher
// will call OnEvent on state transitions (new, escalation); it is the operator's
// responsibility to act on the recommendation.
type ActionHook interface {
	OnEvent(ctx context.Context, ev store.Event) error
}

// NoOpHook logs events via structured logging without dispatching to any external system.
// It is the default hook when no NOTIFY_WEBHOOK_URL is configured.
type NoOpHook struct{ Log *slog.Logger }

func (h *NoOpHook) OnEvent(_ context.Context, ev store.Event) error {
	h.Log.Info("action hook (no-op)",
		"event_type", ev.EventType,
		"metric", ev.MetricName,
		"severity", ev.Severity,
		"rule", ev.RuleName,
	)
	return nil
}

// ScaleHintWebhook POSTs a human-readable scaling recommendation to a webhook URL.
// It does NOT change any infrastructure state — it is purely informational.
// Payload conforms to a Slack-compatible incoming webhook schema so it can be
// wired to Slack, PagerDuty Webhooks, or any generic HTTP receiver.
type ScaleHintWebhook struct {
	URL  string
	Log  *slog.Logger
	http *http.Client
}

// NewScaleHintWebhook creates a ScaleHintWebhook that POSTs to url.
func NewScaleHintWebhook(url string, log *slog.Logger) *ScaleHintWebhook {
	return &ScaleHintWebhook{
		URL:  url,
		Log:  log,
		http: &http.Client{Timeout: 10 * time.Second},
	}
}

func (h *ScaleHintWebhook) OnEvent(ctx context.Context, ev store.Event) error {
	text := buildText(ev)

	payload := map[string]any{
		"text":       text,
		"metric":     ev.MetricName,
		"severity":   ev.Severity,
		"rule":       ev.RuleName,
		"event_type": ev.EventType,
		"timestamp":  ev.Timestamp.UTC().Format(time.RFC3339),
	}
	if ev.EventType == "predictive" && ev.PredictedCrossingTime != nil {
		payload["predicted_crossing_time"] = *ev.PredictedCrossingTime
	}

	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.URL, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build webhook request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := h.http.Do(req)
	if err != nil {
		return fmt.Errorf("webhook POST: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return fmt.Errorf("webhook returned HTTP %d", resp.StatusCode)
	}
	h.Log.Info("scale hint dispatched",
		"metric", ev.MetricName,
		"event_type", ev.EventType,
		"url", h.URL,
	)
	return nil
}

func buildText(ev store.Event) string {
	switch ev.EventType {
	case "predictive":
		horizon := ev.ForecastHorizon
		if horizon == "" {
			horizon = "unknown horizon"
		}
		conf := ""
		if ev.Confidence != nil {
			conf = fmt.Sprintf(", confidence: %.0f%%", *ev.Confidence*100)
		}
		return fmt.Sprintf(
			"[FORECAST] %s is projected to breach capacity within %s%s (severity: %s). HUMAN ACTION RECOMMENDED — review capacity headroom.",
			ev.MetricName, horizon, conf, ev.Severity,
		)
	default:
		return fmt.Sprintf(
			"[ANOMALY] %s: rule=%s, severity=%s, observed=%.2f. Review for operational impact.",
			ev.MetricName, ev.RuleName, ev.Severity, ev.ObservedValue,
		)
	}
}
