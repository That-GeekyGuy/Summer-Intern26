package sim

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

// SequenceStep is one entry in a scenario sequence file.
type SequenceStep struct {
	Mode     string        `yaml:"mode"`
	Duration time.Duration `yaml:"duration"`
}

// SequenceConfig is the top-level structure of the sequence YAML file.
type SequenceConfig struct {
	Steps []SequenceStep `yaml:"sequence"`
}

// LoadSequence parses a sequence YAML file.
func LoadSequence(path string) (*SequenceConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read sequence file: %w", err)
	}
	var cfg SequenceConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse sequence file: %w", err)
	}
	for i, s := range cfg.Steps {
		if s.Duration <= 0 {
			return nil, fmt.Errorf("step %d (%s): duration must be > 0", i, s.Mode)
		}
		if !validMode(Mode(s.Mode)) {
			return nil, fmt.Errorf("step %d: unknown mode %q", i, s.Mode)
		}
	}
	return &cfg, nil
}

// Scheduler advances through a SequenceConfig in a loop.
// A manual SetMode call on the Engine immediately overrides the scheduler,
// but the scheduler will continue from the current step when the override
// expires. To hard-stop the scheduler, cancel the context.
type Scheduler struct {
	engine *Engine
	steps  []SequenceStep
	log    *slog.Logger
}

// NewScheduler creates a Scheduler for the given sequence.
func NewScheduler(engine *Engine, cfg *SequenceConfig, log *slog.Logger) *Scheduler {
	return &Scheduler{engine: engine, steps: cfg.Steps, log: log}
}

// Run loops through the sequence until ctx is cancelled.
// Each step sets the engine mode for its configured duration, then advances.
func (s *Scheduler) Run(ctx context.Context) {
	s.log.Info("sequence scheduler started", "steps", len(s.steps))
	i := 0
	for {
		step := s.steps[i%len(s.steps)]
		s.engine.SetMode(Mode(step.Mode), step.Duration)
		s.log.Info("sequence step", "index", i%len(s.steps), "mode", step.Mode, "duration", step.Duration)

		select {
		case <-ctx.Done():
			s.log.Info("sequence scheduler stopped")
			return
		case <-time.After(step.Duration):
			i++
		}
	}
}

func validMode(m Mode) bool {
	for _, allowed := range AllModes {
		if m == allowed {
			return true
		}
	}
	return false
}
