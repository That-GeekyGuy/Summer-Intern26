package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	detclient "bess.internal/upf-analysis/internal/detector"
	"bess.internal/upf-analysis/internal/metrics"
	"bess.internal/upf-analysis/internal/rag"
	"bess.internal/upf-analysis/internal/store"
	"bess.internal/upf-analysis/internal/validator"
	"bess.internal/upf-analysis/internal/vmclient"
)

const systemPrompt = `You are an expert 5G network analyst specializing in User Plane Function (UPF) metrics for BESS-UPF systems. You help network engineers investigate anomalies, understand trends, and diagnose issues in the 5G data plane.

IMPORTANT — always follow these rules:
1. Call get_metric_metadata first when you are unsure of metric names. Never guess or invent metric names.
2. If query_prometheus is rejected with a validation error, read the error carefully and reformulate using only listed metrics.
3. Narrow query scope with label matchers (e.g. {job="upf"}).
4. Report actual numeric values from your queries — never estimate.
5. Check get_anomalies early to understand what the detection service has already flagged for current conditions.
6. REACTIVE events (event_type="reactive"): anomalies happening NOW or recently observed. Use language like "is elevated", "has spiked", "currently exceeds", "was detected at".
   PREDICTIVE events (event_type="predictive"): linear-regression forecasts of FUTURE states — use get_predictions to retrieve them. Use language like "is forecast to", "is projected to breach", "trend suggests will reach", "is on track to exceed". NEVER describe a predictive event as something currently observed or already happening.
7. When asked about capacity headroom, future risk, or projected trends, call get_predictions to retrieve forecast events.
8. Use at most 3 tool calls per response, then synthesize and answer with the data you have. Do not loop indefinitely collecting data.`

// Session holds the conversation history for one chat session.
type Session struct {
	mu         sync.Mutex
	ID         string
	Messages   []Message
	LastActive time.Time
}

// SessionStore manages in-memory chat sessions with TTL-based eviction.
type SessionStore struct {
	mu       sync.Mutex
	sessions map[string]*Session
	ttl      time.Duration
	onSize   func(int) // called after add/evict to report current count
}

func newSessionStore(ttl time.Duration, onSize func(int)) *SessionStore {
	if onSize == nil {
		onSize = func(int) {}
	}
	return &SessionStore{sessions: make(map[string]*Session), ttl: ttl, onSize: onSize}
}

func (ss *SessionStore) get(id string) *Session {
	ss.mu.Lock()
	defer ss.mu.Unlock()
	s, ok := ss.sessions[id]
	if !ok {
		s = &Session{
			ID:       id,
			Messages: []Message{SystemMessage(systemPrompt)},
		}
		ss.sessions[id] = s
		ss.onSize(len(ss.sessions))
	}
	s.LastActive = time.Now()
	return s
}

func (ss *SessionStore) startCleanup(ctx context.Context, interval time.Duration) {
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				cutoff := time.Now().Add(-ss.ttl)
				ss.mu.Lock()
				for id, s := range ss.sessions {
					if s.LastActive.Before(cutoff) {
						delete(ss.sessions, id)
					}
				}
				ss.onSize(len(ss.sessions))
				ss.mu.Unlock()
			}
		}
	}()
}

// Allowlist is the subset of DynamicAllowlist that the orchestrator needs.
type Allowlist interface {
	IsAllowed(name string) bool
	Size() int
	Names() []string
}

// ChatResponse is returned from Orchestrator.Chat.
type ChatResponse struct {
	Answer      string
	QueriesUsed []string
	Anomalies   []detclient.AnomalyEvent
}

// Orchestrator drives the LLM agentic tool-calling loop.
type Orchestrator struct {
	llm       *Client
	validator *validator.Validator
	vm        *vmclient.Client
	det       *detclient.Client
	allowlist Allowlist
	rag       rag.Retriever
	audit     *store.AuditLog
	sessions  *SessionStore
	maxIter   int
	log       *slog.Logger
	m         *metrics.M // nil-safe
}

// OrchestratorConfig wires up Orchestrator dependencies.
type OrchestratorConfig struct {
	LLM        *Client
	Validator  *validator.Validator
	VM         *vmclient.Client
	Detector   *detclient.Client
	Allowlist  Allowlist
	RAG        rag.Retriever
	Audit      *store.AuditLog
	SessionTTL time.Duration
	MaxIter    int
	Log        *slog.Logger
	Metrics    *metrics.M // optional; pass nil to disable instrumentation
}

func NewOrchestrator(cfg OrchestratorConfig) *Orchestrator {
	if cfg.MaxIter <= 0 {
		cfg.MaxIter = 8
	}
	if cfg.SessionTTL <= 0 {
		cfg.SessionTTL = 30 * time.Minute
	}

	var onSize func(int)
	if cfg.Metrics != nil {
		m := cfg.Metrics
		onSize = func(n int) { m.ActiveSessions.Set(float64(n)) }
	}

	ss := newSessionStore(cfg.SessionTTL, onSize)
	return &Orchestrator{
		llm:       cfg.LLM,
		validator: cfg.Validator,
		vm:        cfg.VM,
		det:       cfg.Detector,
		allowlist: cfg.Allowlist,
		rag:       cfg.RAG,
		audit:     cfg.Audit,
		sessions:  ss,
		maxIter:   cfg.MaxIter,
		log:       cfg.Log,
		m:         cfg.Metrics,
	}
}

// StartSessionCleanup begins the background session eviction goroutine.
func (o *Orchestrator) StartSessionCleanup(ctx context.Context) {
	o.sessions.startCleanup(ctx, 10*time.Minute)
}

// Chat processes one user turn and returns the assistant response.
func (o *Orchestrator) Chat(ctx context.Context, sessionID, userMessage string) (*ChatResponse, error) {
	session := o.sessions.get(sessionID)
	session.mu.Lock()
	defer session.mu.Unlock()

	// Inject RAG context as a prefixed user message if snippets are found.
	if o.rag != nil {
		if snips := o.rag.Retrieve(userMessage, 3); len(snips) > 0 {
			var sb strings.Builder
			sb.WriteString("[Background context from runbooks]\n")
			for _, s := range snips {
				fmt.Fprintf(&sb, "--- %s ---\n%s\n\n", s.Source, s.Content)
			}
			sb.WriteString("[User question] ")
			sb.WriteString(userMessage)
			enriched := sb.String()
			session.Messages = append(session.Messages, UserMessage(enriched))
		} else {
			session.Messages = append(session.Messages, UserMessage(userMessage))
		}
	} else {
		session.Messages = append(session.Messages, UserMessage(userMessage))
	}

	var queriesUsed []string
	var anomalies []detclient.AnomalyEvent

	for i := 0; i < o.maxIter; i++ {
		// On the last allowed iteration, omit tools to force a text answer
		// and to avoid a context-length 400 from vLLM (tools schema adds ~460 tokens).
		tools := Tools()
		if i == o.maxIter-1 {
			tools = nil
		}
		resp, err := o.llm.Complete(ctx, session.Messages, tools)
		if err != nil {
			return nil, fmt.Errorf("LLM call %d: %w", i+1, err)
		}

		choice := resp.Choices[0]
		msg := choice.Message

		// Fallback: Qwen3 sometimes emits tool calls inside <tool_call>…</tool_call>
		// tags in the content field instead of the structured tool_calls array
		// (typically when finish_reason is "length" due to thinking token overflow).
		// Detect and normalise before storing the message so the loop can execute them.
		if len(msg.ToolCalls) == 0 && msg.Content != nil {
			if extracted := parseTextToolCalls(*msg.Content); len(extracted) > 0 {
				o.log.Debug("text-format tool calls detected", "count", len(extracted))
				msg.ToolCalls = extracted
				msg.Content = nil // tool_calls messages must carry null content
			}
		}

		// Persist assistant turn (may be a tool call or a final answer).
		session.Messages = append(session.Messages, msg)

		if len(msg.ToolCalls) == 0 {
			// Reset session to [sys, user, answer] — keeps exactly one turn of
			// context for follow-up questions without accumulating across turns.
			// Store the original user text (not RAG-enriched) to avoid bloat.
			session.Messages = []Message{session.Messages[0], UserMessage(userMessage), msg}

			answer := ""
			if msg.Content != nil {
				answer = *msg.Content
			}
			// Best-effort audit log.
			_ = o.audit.Log(ctx, store.AuditEntry{
				SessionID:       sessionID,
				UserMessage:     userMessage,
				ExecutionStatus: "ok",
			})
			return &ChatResponse{
				Answer:      answer,
				QueriesUsed: queriesUsed,
				Anomalies:   anomalies,
			}, nil
		}

		// Execute each tool call and append results.
		for _, call := range msg.ToolCalls {
			if o.m != nil {
				o.m.ToolCalls.WithLabelValues(call.Function.Name).Inc()
			}
			result, execErr := o.executeTool(ctx, call, sessionID, userMessage, &queriesUsed, &anomalies)
			if execErr != nil {
				o.log.Error("tool execution error", "tool", call.Function.Name, "err", execErr)
				errJSON, _ := json.Marshal(map[string]string{"error": execErr.Error()})
				result = string(errJSON)
			}
			session.Messages = append(session.Messages, ToolResultMessage(call.ID, result))
		}
	}

	return nil, fmt.Errorf("exceeded maximum iterations (%d) without final answer", o.maxIter)
}

// executeTool dispatches a single tool call and returns the JSON result string.
func (o *Orchestrator) executeTool(ctx context.Context, call ToolCall,
	sessionID, userMessage string,
	queriesUsed *[]string, anomalies *[]detclient.AnomalyEvent,
) (string, error) {
	switch call.Function.Name {
	case ToolQueryPrometheus:
		return o.execQueryPrometheus(ctx, call, sessionID, userMessage, queriesUsed)
	case ToolGetAnomalies:
		return o.execGetAnomalies(ctx, call, anomalies)
	case ToolGetPredictions:
		return o.execGetPredictions(ctx, call, anomalies)
	case ToolGetMetricMetadata:
		return o.execGetMetricMetadata()
	default:
		return fmt.Sprintf(`{"error":"unknown tool %q"}`, call.Function.Name), nil
	}
}

type queryPrometheusArgs struct {
	PromQL    string `json:"promql"`
	TimeRange string `json:"time_range"`
	Step      string `json:"step"`
}

func (o *Orchestrator) execQueryPrometheus(ctx context.Context, call ToolCall,
	sessionID, userMessage string, queriesUsed *[]string,
) (string, error) {
	var args queryPrometheusArgs
	if err := json.Unmarshal([]byte(call.Function.Arguments), &args); err != nil {
		return jsonError("invalid arguments: " + err.Error()), nil
	}

	timeRange, err := parseDuration(args.TimeRange)
	if err != nil {
		return jsonError("invalid time_range: " + err.Error()), nil
	}
	step := time.Minute
	if args.Step != "" {
		if s, err := parseDuration(args.Step); err == nil {
			step = s
		}
	}

	// Validate before execution — this is the security-critical path.
	valErr := o.validator.Validate(args.PromQL, timeRange, step)
	auditEntry := store.AuditEntry{
		SessionID:   sessionID,
		UserMessage: userMessage,
		ToolName:    call.Function.Name,
		ToolArgs:    call.Function.Arguments,
	}
	if valErr != nil {
		auditEntry.ValidationError = valErr.Error()
		auditEntry.ExecutionStatus = "rejected"
		_ = o.audit.Log(ctx, auditEntry)

		if o.m != nil {
			reason := classifyValidationError(valErr.Error())
			o.m.ValidatorRejects.WithLabelValues(reason).Inc()
		}

		return jsonError("PromQL validation failed: " + valErr.Error()), nil
	}

	result, err := o.vm.QueryRange(ctx, args.PromQL, timeRange, step, 10)
	if err != nil {
		auditEntry.ExecutionStatus = "vm_error: " + err.Error()
		_ = o.audit.Log(ctx, auditEntry)
		return jsonError("query execution failed: " + err.Error()), nil
	}

	auditEntry.ExecutionStatus = "ok"
	auditEntry.RowCount = result.SeriesCount
	_ = o.audit.Log(ctx, auditEntry)

	*queriesUsed = append(*queriesUsed, args.PromQL)

	b, _ := json.Marshal(result)
	return string(b), nil
}

// classifyValidationError maps a validator error message to a short reason label
// suitable for use as a Prometheus label value.
func classifyValidationError(msg string) string {
	lower := strings.ToLower(msg)
	switch {
	case strings.Contains(lower, "syntax"):
		return "syntax"
	case strings.Contains(lower, "allowlist") || strings.Contains(lower, "not allowed"):
		return "allowlist"
	case strings.Contains(lower, "time range") || strings.Contains(lower, "range"):
		return "time_range"
	case strings.Contains(lower, "step") || strings.Contains(lower, "resolution"):
		return "step"
	case strings.Contains(lower, "fan-out") || strings.Contains(lower, "cardinality"):
		return "fan_out"
	case strings.Contains(lower, "selector"):
		return "selector"
	default:
		return "other"
	}
}

type getAnomaliesArgs struct {
	Since    string `json:"since"`
	Metric   string `json:"metric"`
	Severity string `json:"severity"`
}

func (o *Orchestrator) execGetAnomalies(ctx context.Context, call ToolCall, anomalies *[]detclient.AnomalyEvent) (string, error) {
	var args getAnomaliesArgs
	if err := json.Unmarshal([]byte(call.Function.Arguments), &args); err != nil {
		return jsonError("invalid arguments: " + err.Error()), nil
	}
	since, err := parseDuration(args.Since)
	if err != nil {
		return jsonError("invalid since: " + err.Error()), nil
	}

	events, err := o.det.GetAnomalies(ctx, since, args.Metric, args.Severity)
	if err != nil {
		return jsonError("detection service error: " + err.Error()), nil
	}
	*anomalies = append(*anomalies, events...)

	shown := events
	if len(shown) > 5 {
		shown = shown[len(shown)-5:] // keep 5 most recent
	}
	b, _ := json.Marshal(map[string]any{"anomalies": shown, "total": len(events)})
	return string(b), nil
}

type getPredictionsArgs struct {
	Since  string `json:"since"`
	Metric string `json:"metric"`
}

func (o *Orchestrator) execGetPredictions(ctx context.Context, call ToolCall, anomalies *[]detclient.AnomalyEvent) (string, error) {
	var args getPredictionsArgs
	if err := json.Unmarshal([]byte(call.Function.Arguments), &args); err != nil {
		return jsonError("invalid arguments: " + err.Error()), nil
	}
	since, err := parseDuration(args.Since)
	if err != nil {
		return jsonError("invalid since: " + err.Error()), nil
	}

	events, err := o.det.GetPredictions(ctx, since, args.Metric)
	if err != nil {
		return jsonError("detection service error: " + err.Error()), nil
	}
	*anomalies = append(*anomalies, events...)

	shown := events
	if len(shown) > 5 {
		shown = shown[len(shown)-5:]
	}
	b, _ := json.Marshal(map[string]any{
		"predictions": shown,
		"total":       len(events),
		"note":        "These are FORECAST events — projected future states, not currently observed conditions. Use 'is forecast to', 'is projected to breach' language.",
	})
	return string(b), nil
}

// internalMetricPrefixes are scraper/runtime internals not useful to the operator.
var internalMetricPrefixes = []string{
	"go_", "process_", "prometheus_", "promhttp_",
	"net_conntrack_", "scrape_",
}

// internalMetricExact are exact metric names that are internal scrape metadata.
var internalMetricExact = map[string]struct{}{
	"up": {},
}

func isInternalMetric(name string) bool {
	if _, exact := internalMetricExact[name]; exact {
		return true
	}
	for _, p := range internalMetricPrefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

func (o *Orchestrator) execGetMetricMetadata() (string, error) {
	all := o.allowlist.Names()
	var operator []string
	for _, n := range all {
		if !isInternalMetric(n) {
			operator = append(operator, n)
		}
	}
	b, _ := json.Marshal(map[string]any{
		"metrics": operator,
		"total":   len(operator),
		"note":    "Use these exact names in query_prometheus. Internal runtime metrics (go_*, prometheus_*, process_*) are omitted.",
	})
	return string(b), nil
}

// parseTextToolCalls extracts ToolCalls from Hermes-format text when the model
// emits <tool_call>{"name":"…","arguments":{…}}</tool_call> in content instead
// of the structured tool_calls field. Each extracted call gets a synthetic ID.
func parseTextToolCalls(content string) []ToolCall {
	const open, close = "<tool_call>", "</tool_call>"
	var calls []ToolCall
	s := content
	for {
		i := strings.Index(s, open)
		if i < 0 {
			break
		}
		s = s[i+len(open):]
		j := strings.Index(s, close)
		raw := s
		if j >= 0 {
			raw = s[:j]
			s = s[j+len(close):]
		}
		raw = strings.TrimSpace(raw)
		var parsed struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal([]byte(raw), &parsed); err != nil || parsed.Name == "" {
			if j < 0 {
				break
			}
			continue
		}
		args := string(parsed.Arguments)
		if args == "" || args == "null" {
			args = "{}"
		}
		calls = append(calls, ToolCall{
			ID:   fmt.Sprintf("fallback-%d", len(calls)),
			Type: "function",
			Function: FunctionCall{Name: parsed.Name, Arguments: args},
		})
		if j < 0 {
			break
		}
	}
	return calls
}

func jsonError(msg string) string {
	b, _ := json.Marshal(map[string]string{"error": msg})
	return string(b)
}

// parseDuration extends time.ParseDuration to handle "Xd" (days) shorthand.
func parseDuration(s string) (time.Duration, error) {
	if strings.HasSuffix(s, "d") {
		n := strings.TrimSuffix(s, "d")
		days := 0
		if _, err := fmt.Sscanf(n, "%d", &days); err == nil && days > 0 {
			return time.Duration(days) * 24 * time.Hour, nil
		}
	}
	return time.ParseDuration(s)
}
