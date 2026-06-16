package validator

import (
	"errors"
	"fmt"
	"time"

	"github.com/prometheus/prometheus/promql/parser"
)

// Allowlist determines which metric names may appear in a query.
type Allowlist interface {
	IsAllowed(name string) bool
}

// Validator is the primary security boundary between the LLM and VictoriaMetrics.
// It rejects queries that reference unknown metrics, use excessive time ranges,
// or use steps so small they could produce resource-exhausting result sets.
type Validator struct {
	allowlist    Allowlist
	maxTimeRange time.Duration
	minStep      time.Duration
}

// New creates a Validator with the given policy limits.
func New(allowlist Allowlist, maxTimeRange, minStep time.Duration) *Validator {
	return &Validator{allowlist: allowlist, maxTimeRange: maxTimeRange, minStep: minStep}
}

// Validate returns a non-nil error if the expression or parameters violate any policy.
// Checks are applied in priority order: time range → step floor → parse → AST walk.
func (v *Validator) Validate(promql string, timeRange, step time.Duration) error {
	if timeRange > v.maxTimeRange {
		return fmt.Errorf("time range %v exceeds maximum allowed %v", timeRange, v.maxTimeRange)
	}
	if step < v.minStep {
		return fmt.Errorf("step %v is below minimum allowed %v", step, v.minStep)
	}
	expr, err := parser.ParseExpr(promql)
	if err != nil {
		return fmt.Errorf("parse error: %w", err)
	}
	return v.walkAST(expr)
}

// walkAST visits every VectorSelector node in the expression tree.
// It rejects selectors without an explicit metric name (fan-out prevention) and
// rejects metric names absent from the allowlist.
func (v *Validator) walkAST(expr parser.Expr) error {
	var validationErr error
	parser.Inspect(expr, func(node parser.Node, _ []parser.Node) error {
		if validationErr != nil {
			return validationErr // stop traversal early on first violation
		}
		vs, ok := node.(*parser.VectorSelector)
		if !ok {
			return nil
		}
		if vs.Name == "" {
			// {label="value"} without a metric name can match every series — block it.
			validationErr = errors.New("selector without explicit metric name is not permitted; use get_metric_metadata to list available metrics")
			return validationErr
		}
		if !v.allowlist.IsAllowed(vs.Name) {
			validationErr = fmt.Errorf("metric %q is not in the allowed list; use get_metric_metadata to see available metrics", vs.Name)
			return validationErr
		}
		return nil
	})
	return validationErr
}
