from __future__ import annotations
import time
from collections import defaultdict

from mitigation.policy import ActionClass


class RateLimiter:
    def __init__(self, max_per_window: int = 3, window_seconds: float = 300.0) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._hits: dict[tuple, list[float]] = defaultdict(list)

    def check(self, action_class: ActionClass, upf_id: str) -> bool:
        key = (action_class, upf_id)
        now = time.monotonic()
        self._hits[key] = [t for t in self._hits[key] if now - t < self._window]
        if len(self._hits[key]) >= self._max:
            return False
        self._hits[key].append(now)
        return True


class BlastRadiusGuard:
    def __init__(self, max_upfs: int = 5, window_seconds: float = 300.0) -> None:
        self._max = max_upfs
        self._window = window_seconds
        self._events: dict[ActionClass, list[tuple[float, str]]] = defaultdict(list)

    def check(self, action_class: ActionClass, upf_id: str) -> bool:
        now = time.monotonic()
        self._events[action_class] = [
            (t, u) for t, u in self._events[action_class] if now - t < self._window
        ]
        seen = {u for _, u in self._events[action_class]}
        if upf_id not in seen and len(seen) >= self._max:
            return False
        self._events[action_class].append((now, upf_id))
        return True


class GuardrailsEngine:
    def __init__(self, rate_limiter: RateLimiter, blast_guard: BlastRadiusGuard) -> None:
        self._rl = rate_limiter
        self._bg = blast_guard

    def check(self, action_class: ActionClass, upf_id: str) -> bool:
        # ponytail: short-circuit — blast_guard.check() has side effects, so rate limiter goes first
        return self._rl.check(action_class, upf_id) and self._bg.check(action_class, upf_id)
