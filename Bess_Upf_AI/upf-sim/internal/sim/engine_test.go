package sim

import (
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// noopLogger satisfies the *slog.Logger requirement without real I/O.
// We use nil here because the engine gracefully handles a nil logger
// for the log.Info calls (they are guarded in production; we accept panics
// in tests as a signal to add guards if they occur).
func newTestEngine() *Engine {
	reg := prometheus.NewRegistry()
	cfg := DefaultConfig("test-node")
	// Use a nil slog — tests don't need log output.
	return NewEngine(cfg, reg, nil)
}

// anchoredTime returns a time.Time with a specific hour for diurnal tests.
func anchoredTime(hour int) time.Time {
	return time.Date(2024, 1, 15, hour, 0, 0, 0, time.UTC)
}

// --- Normal mode ---

func TestNormal_SessionsInDiurnalRange(t *testing.T) {
	e := newTestEngine()

	// Peak (14:00) and trough (02:00) should be within expected diurnal bounds.
	for _, tc := range []struct {
		hour int
		minS float64
		maxS float64
	}{
		{hour: 14, minS: 13_000, maxS: 17_000}, // peak ≈ 1.3× base, ±jitter
		{hour: 2,  minS: 7_000,  maxS: 10_500}, // trough ≈ 0.7× base, ±jitter
		{hour: 8,  minS: 9_000,  maxS: 14_000}, // midpoint ≈ 1.0× base, ±jitter
	} {
		r := e.normalRates(anchoredTime(tc.hour))
		if r.sessions < tc.minS || r.sessions > tc.maxS {
			t.Errorf("hour=%d: sessions %.0f not in [%.0f, %.0f]",
				tc.hour, r.sessions, tc.minS, tc.maxS)
		}
	}
}

func TestNormal_ByteCountersPositive(t *testing.T) {
	e := newTestEngine()
	r := e.normalRates(anchoredTime(12))
	for _, key := range []string{"rx/N3", "tx/N3", "rx/N6", "tx/N6"} {
		if r.bytesRate[key] <= 0 {
			t.Errorf("bytesRate[%s] = %.2f, want > 0", key, r.bytesRate[key])
		}
		if r.pktsRate[key] <= 0 {
			t.Errorf("pktsRate[%s] = %.2f, want > 0", key, r.pktsRate[key])
		}
	}
}

func TestNormal_DropRateLow(t *testing.T) {
	e := newTestEngine()
	r := e.normalRates(anchoredTime(12))
	// Total drops should be a tiny fraction of total packets.
	var totalPkts, totalDrops float64
	for _, key := range []string{"rx/N3", "tx/N3", "rx/N6", "tx/N6"} {
		totalPkts += r.pktsRate[key]
		totalDrops += r.dropRate[key]
	}
	ratio := totalDrops / totalPkts
	if ratio > 0.001 { // expect < 0.1 %
		t.Errorf("normal drop ratio %.4f exceeds 0.001", ratio)
	}
}

// --- Session spike ---

func TestSessionSpike_HoldsAtMultiplierAfterRamp(t *testing.T) {
	e := newTestEngine()
	e.state = ScenarioState{Mode: ModeSessionSpike, StartedAt: time.Now().Add(-200 * time.Second)}

	r := e.computeRates(time.Now())
	base := e.normalRates(time.Now())

	ratio := r.sessions / base.sessions
	if ratio < 3.5 || ratio > 4.5 {
		t.Errorf("post-ramp spike ratio %.2f not in [3.5, 4.5]", ratio)
	}
}

func TestSessionSpike_RampsGradually(t *testing.T) {
	e := newTestEngine()
	start := time.Now()
	e.state = ScenarioState{Mode: ModeSessionSpike, StartedAt: start}

	// At t=0 the ramp hasn't started, multiplier should be ≈1.
	r0 := e.computeRates(start)
	base := e.normalRates(start)
	ratio0 := r0.sessions / base.sessions
	if ratio0 < 0.9 || ratio0 > 1.1 {
		t.Errorf("ramp t=0 ratio %.2f not ≈ 1.0", ratio0)
	}

	// At t=60 s (halfway) the multiplier should be ≈2.5 (midpoint of 1→4).
	r60 := e.computeRates(start.Add(60 * time.Second))
	ratio60 := r60.sessions / base.sessions
	if ratio60 < 2.0 || ratio60 > 3.0 {
		t.Errorf("ramp t=60 ratio %.2f not in [2.0, 3.0]", ratio60)
	}
}

// --- Session drop ---

func TestSessionDrop_SessionsFallBelowThreshold(t *testing.T) {
	e := newTestEngine()
	start := time.Now()
	e.state = ScenarioState{Mode: ModeSessionDrop, StartedAt: start.Add(-60 * time.Second)}

	r := e.computeRates(time.Now())
	if r.sessions > 1_000 {
		t.Errorf("post-drop sessions %.0f should be < 1000", r.sessions)
	}
}

func TestSessionDrop_ByteRatesProportional(t *testing.T) {
	e := newTestEngine()
	base := e.normalRates(anchoredTime(12))
	e.state = ScenarioState{Mode: ModeSessionDrop, StartedAt: anchoredTime(12).Add(-60 * time.Second)}
	drop := e.computeRates(anchoredTime(12))

	// Bytes rate should be significantly less than normal.
	if drop.bytesRate["rx/N3"] >= base.bytesRate["rx/N3"]*0.5 {
		t.Errorf("drop mode N3-rx bytes %.0f not significantly below normal %.0f",
			drop.bytesRate["rx/N3"], base.bytesRate["rx/N3"])
	}
}

// --- Packet drop surge ---

func TestPacketDropSurge_DropsHighOnN3Rx(t *testing.T) {
	e := newTestEngine()
	e.state = ScenarioState{Mode: ModePacketDropSurge, StartedAt: time.Now()}
	r := e.computeRates(anchoredTime(12))

	// N3 rx drop rate should be ≈ 5 % of packet rate.
	expected := r.pktsRate["rx/N3"] * 0.05
	if r.dropRate["rx/N3"] < expected*0.9 || r.dropRate["rx/N3"] > expected*1.1 {
		t.Errorf("N3-rx drop rate %.2f not ≈ 5%% of pkt rate (%.2f)", r.dropRate["rx/N3"], expected)
	}
}

func TestPacketDropSurge_OtherInterfacesUnchanged(t *testing.T) {
	e := newTestEngine()
	base := e.normalRates(anchoredTime(12))
	e.state = ScenarioState{Mode: ModePacketDropSurge, StartedAt: time.Now()}
	surge := e.computeRates(anchoredTime(12))

	for _, key := range []string{"tx/N3", "rx/N6", "tx/N6"} {
		ratio := surge.dropRate[key] / base.dropRate[key]
		if ratio < 0.5 || ratio > 2.0 {
			t.Errorf("drop rate on %s changed during packet_drop_surge (ratio %.2f)", key, ratio)
		}
	}
}

// --- Asymmetric traffic ---

func TestAsymmetric_N3RxElevated(t *testing.T) {
	e := newTestEngine()
	base := e.normalRates(anchoredTime(12))
	e.state = ScenarioState{Mode: ModeAsymmetric, StartedAt: time.Now()}
	asym := e.computeRates(anchoredTime(12))

	if asym.bytesRate["rx/N3"] < base.bytesRate["rx/N3"]*4.0 {
		t.Errorf("N3 rx bytes not elevated enough (%.0f vs base %.0f)",
			asym.bytesRate["rx/N3"], base.bytesRate["rx/N3"])
	}
}

func TestAsymmetric_N6TxSuppressed(t *testing.T) {
	e := newTestEngine()
	base := e.normalRates(anchoredTime(12))
	e.state = ScenarioState{Mode: ModeAsymmetric, StartedAt: time.Now()}
	asym := e.computeRates(anchoredTime(12))

	if asym.bytesRate["tx/N6"] > base.bytesRate["tx/N6"]*0.2 {
		t.Errorf("N6 tx bytes not suppressed enough (%.0f vs base %.0f)",
			asym.bytesRate["tx/N6"], base.bytesRate["tx/N6"])
	}
}

// --- Flatline ---

func TestFlatline_AllRatesZero(t *testing.T) {
	e := newTestEngine()
	e.frozen = 12_000
	e.state = ScenarioState{Mode: ModeFlatline, StartedAt: time.Now()}
	r := e.computeRates(time.Now())

	for _, key := range []string{"rx/N3", "tx/N3", "rx/N6", "tx/N6"} {
		if r.bytesRate[key] != 0 {
			t.Errorf("flatline bytesRate[%s] = %.2f, want 0", key, r.bytesRate[key])
		}
	}
}

func TestFlatline_SessionsFrozen(t *testing.T) {
	e := newTestEngine()
	e.frozen = 11_500
	e.state = ScenarioState{Mode: ModeFlatline, StartedAt: time.Now()}
	r := e.computeRates(time.Now())

	if r.sessions != 11_500 {
		t.Errorf("flatline sessions = %.0f, want 11500", r.sessions)
	}
}

// --- Auto-revert ---

func TestAutoRevert_NormalAfterDuration(t *testing.T) {
	e := newTestEngine()
	// Set spike with 1-second duration, started 2 seconds ago.
	e.state = ScenarioState{
		Mode:      ModeSessionSpike,
		StartedAt: time.Now().Add(-2 * time.Second),
		Duration:  time.Second,
	}

	// A Tick should trigger the revert.
	e.lastTick = time.Now().Add(-time.Second)
	e.Tick(time.Now())

	if e.state.Mode != ModeNormal {
		t.Errorf("expected auto-revert to normal, got %s", e.state.Mode)
	}
}

// --- SetMode concurrency smoke test ---

func TestSetMode_ConcurrentSafe(t *testing.T) {
	e := newTestEngine()
	done := make(chan struct{})

	go func() {
		for i := 0; i < 100; i++ {
			e.SetMode(ModeSessionSpike, 0)
			e.SetMode(ModeNormal, 0)
		}
		close(done)
	}()

	for i := 0; i < 100; i++ {
		e.CurrentState()
	}
	<-done
}
