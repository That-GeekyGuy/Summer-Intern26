// Package sim implements a time-varying UPF metrics simulator.
// The engine maintains an internal scenario state and advances a tick loop
// every second, computing per-tick deltas and updating Prometheus metrics.
//
// Counter semantics: prometheus/client_golang Counters can only increase.
// The engine models "rate" (bytes/s, packets/s) and calls counter.Add(rate*dt)
// each tick — rate drops to 0 in flatline mode, not the counter itself.
package sim

import (
	"math"
	"math/rand"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"log/slog"
)

// Mode names match the /scenario API and the sequence YAML.
type Mode string

const (
	ModeNormal          Mode = "normal"
	ModeSessionSpike    Mode = "session_spike"
	ModeSessionDrop     Mode = "session_drop"
	ModePacketDropSurge Mode = "packet_drop_surge"
	ModeAsymmetric      Mode = "asymmetric_traffic"
	ModeFlatline        Mode = "flatline"
)

var AllModes = []Mode{
	ModeNormal, ModeSessionSpike, ModeSessionDrop,
	ModePacketDropSurge, ModeAsymmetric, ModeFlatline,
}

// Config holds baseline simulation parameters (all tunable via env).
type Config struct {
	NodeID          string  // value of the node_id label
	BaseSessions    float64 // midpoint session count (~12 000)
	BytesPerSession float64 // bytes/s per session in normal mode (1 000)
	AvgPacketSize   float64 // bytes; used to derive packet rate (800)
	BaseDropRate    float64 // fraction of packets dropped normally (0.000 1)
	SpikeMultiplier float64 // session multiplier for session_spike (4.0)
	RampSeconds     float64 // ramp duration for session_spike (120 s)
}

func DefaultConfig(nodeID string) Config {
	return Config{
		NodeID:          nodeID,
		BaseSessions:    12_000,
		BytesPerSession: 1_000,
		AvgPacketSize:   800,
		BaseDropRate:    0.0001,
		SpikeMultiplier: 4.0,
		RampSeconds:     120,
	}
}

// ScenarioState is a snapshot of the current scenario (safe to copy).
type ScenarioState struct {
	Mode      Mode
	StartedAt time.Time
	Duration  time.Duration // 0 = indefinite / manual
}

// tickRates holds per-second rates for one simulation tick.
type tickRates struct {
	sessions  float64
	bytesRate map[string]float64 // bytes/s, key = "dir/iface"
	pktsRate  map[string]float64
	dropRate  map[string]float64
}

// Engine runs the scenario state machine and updates Prometheus metrics.
// All exported methods are safe for concurrent use.
type Engine struct {
	mu     sync.RWMutex
	state  ScenarioState
	cfg    Config
	frozen float64   // session count frozen at flatline entry
	lastTick time.Time
	log    *slog.Logger

	// Part 4: engine exposes these for the self-observability layer to wrap.
	sessionsGauge *prometheus.GaugeVec
	scenarioGauge *prometheus.GaugeVec
	bytesCounter  *prometheus.CounterVec
	pktsCounter   *prometheus.CounterVec
	dropCounter   *prometheus.CounterVec
}

// NewEngine registers all Prometheus metrics and returns a ready Engine.
func NewEngine(cfg Config, reg prometheus.Registerer, log *slog.Logger) *Engine {
	sessions := prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "pfcp_sessions_total",
		Help: "Current active PFCP sessions on this UPF node.",
	}, []string{"node_id"})

	// upf_sim_scenario: 1 for the currently active mode, 0 for all others.
	// This lets you draw scenario state on a Grafana timeline panel.
	scenario := prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "upf_sim_scenario",
		Help: "Active scenario flag (1 = active, 0 = inactive) labelled by mode name.",
	}, []string{"mode"})

	bytes_ := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "port_bytes_count",
		Help: "Total bytes transferred through each UPF port direction.",
	}, []string{"dir", "iface", "node_id"})

	pkts := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "port_packets_count",
		Help: "Total packets transferred through each UPF port direction.",
	}, []string{"dir", "iface", "node_id"})

	drops := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "port_dropped_count",
		Help: "Total packets dropped at each UPF port direction.",
	}, []string{"dir", "iface", "node_id"})

	reg.MustRegister(sessions, scenario, bytes_, pkts, drops)

	e := &Engine{
		state:         ScenarioState{Mode: ModeNormal, StartedAt: time.Now()},
		cfg:           cfg,
		frozen:        cfg.BaseSessions,
		lastTick:      time.Now(),
		log:           log,
		sessionsGauge: sessions,
		scenarioGauge: scenario,
		bytesCounter:  bytes_,
		pktsCounter:   pkts,
		dropCounter:   drops,
	}

	// Initialise all scenario gauge labels to 0 so they appear in /metrics
	// immediately (before any tick), avoiding "missing series" gaps in VM.
	for _, m := range AllModes {
		scenario.WithLabelValues(string(m)).Set(0)
	}
	scenario.WithLabelValues(string(ModeNormal)).Set(1)

	return e
}

// SetMode switches to the given scenario mode.
// duration=0 means indefinite (manual override — no auto-revert).
func (e *Engine) SetMode(mode Mode, duration time.Duration) {
	e.mu.Lock()
	defer e.mu.Unlock()

	old := e.state.Mode
	// Snapshot sessions so flatline freezes at current level, not default.
	e.frozen = e.lastKnownSessions()

	e.state = ScenarioState{Mode: mode, StartedAt: time.Now(), Duration: duration}
	if e.log != nil { e.log.Info("scenario transition", "from", string(old), "to", string(mode),
		"duration", duration, "at", time.Now().Format(time.RFC3339)) }
}

// CurrentState returns a copy of the current scenario state.
func (e *Engine) CurrentState() ScenarioState {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.state
}

// Tick advances the simulation by one step.
// Call this every ~1 s from a ticker goroutine.
func (e *Engine) Tick(now time.Time) {
	e.mu.Lock()
	defer e.mu.Unlock()

	dt := now.Sub(e.lastTick).Seconds()
	if dt <= 0 || dt > 10 {
		dt = 1.0 // clamp: ignore clock jumps or first-tick edge cases
	}
	e.lastTick = now

	// Auto-revert to normal when a timed scenario expires.
	if e.state.Duration > 0 && now.Sub(e.state.StartedAt) >= e.state.Duration {
		if e.log != nil { e.log.Info("scenario auto-reverted to normal",
			"previous", string(e.state.Mode),
			"at", now.Format(time.RFC3339)) }
		e.state = ScenarioState{Mode: ModeNormal, StartedAt: now, Duration: 0}
	}

	r := e.computeRates(now)

	// Update scenario flag gauges.
	for _, m := range AllModes {
		v := 0.0
		if m == e.state.Mode {
			v = 1.0
		}
		e.scenarioGauge.WithLabelValues(string(m)).Set(v)
	}

	// Sessions gauge (can go up and down freely).
	e.sessionsGauge.WithLabelValues(e.cfg.NodeID).Set(r.sessions)

	// Counter increments: rate × elapsed seconds.
	for _, iface := range []string{"N3", "N6"} {
		for _, dir := range []string{"rx", "tx"} {
			key := dir + "/" + iface
			e.bytesCounter.WithLabelValues(dir, iface, e.cfg.NodeID).Add(r.bytesRate[key] * dt)
			e.pktsCounter.WithLabelValues(dir, iface, e.cfg.NodeID).Add(r.pktsRate[key] * dt)
			e.dropCounter.WithLabelValues(dir, iface, e.cfg.NodeID).Add(r.dropRate[key] * dt)
		}
	}
}

// computeRates returns the per-second rates for the current scenario mode.
// Called with e.mu held.
func (e *Engine) computeRates(now time.Time) tickRates {
	base := e.normalRates(now)

	switch e.state.Mode {
	case ModeNormal:
		return base

	case ModeSessionSpike:
		// Linear ramp from 1× to SpikeMultiplier× over RampSeconds, then hold.
		elapsed := now.Sub(e.state.StartedAt).Seconds()
		mult := e.cfg.SpikeMultiplier
		if elapsed < e.cfg.RampSeconds {
			mult = 1.0 + (e.cfg.SpikeMultiplier-1.0)*(elapsed/e.cfg.RampSeconds)
		}
		return scaleAll(base, mult)

	case ModeSessionDrop:
		// Sessions fall toward ~500 over 30 s then hold (UPF failure/handover storm).
		elapsed := now.Sub(e.state.StartedAt).Seconds()
		const rampSecs = 30.0
		target := 500.0 / base.sessions // fraction of normal
		frac := 1.0
		if elapsed < rampSecs {
			frac = 1.0 - (1.0-target)*(elapsed/rampSecs)
		} else {
			frac = target
		}
		if frac < 0.01 {
			frac = 0.01
		}
		return scaleAll(base, frac)

	case ModePacketDropSurge:
		// Drop rate on N3 rx jumps to 5 % of packets; other counters unchanged.
		r := cloneRates(base)
		r.dropRate["rx/N3"] = r.pktsRate["rx/N3"] * 0.05
		return r

	case ModeAsymmetric:
		// N3 rx 5× (traffic surge inbound), N6 tx drops to 10 % (outbound congested).
		r := cloneRates(base)
		r.bytesRate["rx/N3"] *= 5.0
		r.pktsRate["rx/N3"] *= 5.0
		r.dropRate["rx/N3"] *= 5.0
		r.bytesRate["tx/N6"] *= 0.1
		r.pktsRate["tx/N6"] *= 0.1
		return r

	case ModeFlatline:
		// All counters freeze (rates → 0). Sessions stay at last known value.
		// Simulates a stuck/crashed UPF that still answers scrapes.
		r := zeroRates()
		r.sessions = e.frozen
		return r
	}

	return base
}

// normalRates computes baseline rates with a diurnal session pattern.
// Peak sessions at 14:00, trough at 02:00; ±2 % jitter.
func (e *Engine) normalRates(now time.Time) tickRates {
	hour := float64(now.Hour()) + float64(now.Minute())/60.0
	// Shift peak to 14:00 by subtracting 8 from hour (sin peaks at π/2).
	diurnal := 1.0 + 0.3*math.Sin(2*math.Pi*(hour-8.0)/24.0)
	jitter := 1.0 + (rand.Float64()*0.04 - 0.02)
	sessions := e.cfg.BaseSessions * diurnal * jitter

	bps := e.cfg.BytesPerSession               // bytes/s per session
	pps := bps / e.cfg.AvgPacketSize           // packets/s per session
	dps := pps * e.cfg.BaseDropRate            // drops/s per session

	// N3 (RAN-facing) carries slightly more traffic than N6 (internet-facing).
	return tickRates{
		sessions: sessions,
		bytesRate: map[string]float64{
			"rx/N3": sessions * bps * 1.2,
			"tx/N3": sessions * bps * 0.8,
			"rx/N6": sessions * bps * 0.8,
			"tx/N6": sessions * bps * 1.2,
		},
		pktsRate: map[string]float64{
			"rx/N3": sessions * pps * 1.2,
			"tx/N3": sessions * pps * 0.8,
			"rx/N6": sessions * pps * 0.8,
			"tx/N6": sessions * pps * 1.2,
		},
		dropRate: map[string]float64{
			"rx/N3": sessions * dps * 1.2,
			"tx/N3": sessions * dps * 0.8,
			"rx/N6": sessions * dps * 0.8,
			"tx/N6": sessions * dps * 1.2,
		},
	}
}

// lastKnownSessions approximates the current session count without a full tick.
// Used to freeze the flatline value at the actual level when transitioning.
func (e *Engine) lastKnownSessions() float64 {
	if e.frozen > 0 {
		return e.frozen
	}
	return e.cfg.BaseSessions
}

func scaleAll(r tickRates, mult float64) tickRates {
	out := cloneRates(r)
	out.sessions = r.sessions * mult
	for k := range out.bytesRate { out.bytesRate[k] *= mult }
	for k := range out.pktsRate  { out.pktsRate[k] *= mult }
	// Drop rate intentionally NOT scaled during spike — only sessions and traffic.
	return out
}

func cloneRates(r tickRates) tickRates {
	out := tickRates{
		sessions: r.sessions,
		bytesRate: make(map[string]float64, len(r.bytesRate)),
		pktsRate:  make(map[string]float64, len(r.pktsRate)),
		dropRate:  make(map[string]float64, len(r.dropRate)),
	}
	for k, v := range r.bytesRate { out.bytesRate[k] = v }
	for k, v := range r.pktsRate  { out.pktsRate[k] = v }
	for k, v := range r.dropRate  { out.dropRate[k] = v }
	return out
}

func zeroRates() tickRates {
	keys := []string{"rx/N3", "tx/N3", "rx/N6", "tx/N6"}
	r := tickRates{
		bytesRate: make(map[string]float64, 4),
		pktsRate:  make(map[string]float64, 4),
		dropRate:  make(map[string]float64, 4),
	}
	for _, k := range keys {
		r.bytesRate[k] = 0
		r.pktsRate[k] = 0
		r.dropRate[k] = 0
	}
	return r
}
