from mitigation.guardrails import RateLimiter, BlastRadiusGuard, GuardrailsEngine
from mitigation.policy import ActionClass


def test_rate_limiter_allows_within_window():
    rl = RateLimiter(max_per_window=3, window_seconds=60.0)
    assert rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert rl.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_rate_limiter_blocks_over_limit():
    rl = RateLimiter(max_per_window=2, window_seconds=60.0)
    rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert not rl.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_blast_radius_allows_within_limit():
    bg = BlastRadiusGuard(max_upfs=3, window_seconds=60.0)
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-2")
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-3")


def test_blast_radius_blocks_over_limit():
    bg = BlastRadiusGuard(max_upfs=2, window_seconds=60.0)
    bg.check(ActionClass.HPA_SCALE_UP, "upf-1")
    bg.check(ActionClass.HPA_SCALE_UP, "upf-2")
    assert not bg.check(ActionClass.HPA_SCALE_UP, "upf-3")


def test_blast_radius_same_upf_does_not_count_twice():
    bg = BlastRadiusGuard(max_upfs=1, window_seconds=60.0)
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert bg.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_guardrails_engine_passes_clean():
    engine = GuardrailsEngine(
        RateLimiter(max_per_window=3, window_seconds=60.0),
        BlastRadiusGuard(max_upfs=5, window_seconds=60.0),
    )
    assert engine.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_guardrails_engine_blocks_on_rate():
    engine = GuardrailsEngine(
        RateLimiter(max_per_window=1, window_seconds=60.0),
        BlastRadiusGuard(max_upfs=5, window_seconds=60.0),
    )
    engine.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert not engine.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_guardrails_engine_blocks_on_blast():
    engine = GuardrailsEngine(
        RateLimiter(max_per_window=10, window_seconds=60.0),
        BlastRadiusGuard(max_upfs=1, window_seconds=60.0),
    )
    engine.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert not engine.check(ActionClass.HPA_SCALE_UP, "upf-2")


def test_rate_limiter_evicts_expired_hits(monkeypatch):
    calls = [0.0]
    monkeypatch.setattr("mitigation.guardrails.time.monotonic", lambda: calls[0])
    rl = RateLimiter(max_per_window=1, window_seconds=10.0)
    rl.check(ActionClass.HPA_SCALE_UP, "upf-1")
    calls[0] = 11.0
    assert rl.check(ActionClass.HPA_SCALE_UP, "upf-1")


def test_blast_radius_does_not_grow_on_repeated_upf():
    bg = BlastRadiusGuard(max_upfs=5, window_seconds=60.0)
    for _ in range(100):
        bg.check(ActionClass.HPA_SCALE_UP, "upf-1")
    assert len(bg._events[ActionClass.HPA_SCALE_UP]) == 1
