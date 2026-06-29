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
	// step == 0 means instant query (no step parameter) — skip the floor check.
	if step > 0 && step < v.minStep {
		return fmt.Errorf("step %v is below minimum allowed %v", step, v.minStep)
	}
	expr, err := parser.ParseExpr(promql)
	if err != nil {
		return fmt.Errorf("parse error: %w", err)
	}
	return v.walkAST(expr)
}

// walkAST visits every node in the expression tree checking:
//   - VectorSelector: must have explicit metric name that is allowlisted
//   - SubqueryExpr: inner step must be ≥ minStep (CPU amplification prevention)
func (v *Validator) walkAST(expr parser.Expr) error {
	var validationErr error
	parser.Inspect(expr, func(node parser.Node, _ []parser.Node) error {
		if validationErr != nil {
			return validationErr
		}
		switch n := node.(type) {
		case *parser.VectorSelector:
			if n.Name == "" {
				validationErr = errors.New("selector without explicit metric name is not permitted; use get_metric_metadata to list available metrics")
				return validationErr
			}
			if !v.allowlist.IsAllowed(n.Name) {
				validationErr = fmt.Errorf("metric %q is not in the allowed list; use get_metric_metadata to see available metrics", n.Name)
				return validationErr
			}
		case *parser.SubqueryExpr:
			// Subquery step controls inner resolution independently of the outer step.
			// A small inner step over a long range causes CPU amplification.
			if n.Step > 0 && n.Step < v.minStep {
				validationErr = fmt.Errorf("subquery step %v is below minimum allowed %v", n.Step, v.minStep)
				return validationErr
			}
		}
		return nil
	})
	return validationErr
}
