"""Per provider-model circuit breaker.

CLOSED  → normal. Failures within the window are counted.
OPEN    → too many failures; short-circuit (skip this model) until cooldown.
HALF_OPEN → cooldown elapsed; allow ONE probe. Success closes, failure re-opens.

In-memory (per replica). Fine for MVP; a distributed variant would keep state
in Redis. State changes are logged and exported to Prometheus.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from app.config import CircuitBreakerConfig
from app.metrics import CIRCUIT_STATE

logger = logging.getLogger("gateway.circuit")

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
_STATE_CODE = {CLOSED: 0, OPEN: 1, HALF_OPEN: 2}


class _Circuit:
    def __init__(self):
        self.state = CLOSED
        self.failures: deque[float] = deque()
        self.opened_at = 0.0
        self.probing = False


class CircuitBreaker:
    def __init__(self, cfg: CircuitBreakerConfig):
        self.cfg = cfg
        self._circuits: dict[str, _Circuit] = {}

    def _get(self, key: str) -> _Circuit:
        c = self._circuits.get(key)
        if c is None:
            c = self._circuits[key] = _Circuit()
        return c

    def _set_state(self, key: str, c: _Circuit, state: str) -> None:
        if c.state != state:
            logger.info("circuit %s: %s -> %s", key, c.state, state)
        c.state = state
        CIRCUIT_STATE.labels(provider_model=key).set(_STATE_CODE[state])

    def allow(self, key: str) -> bool:
        c = self._get(key)
        now = time.time()
        if c.state == OPEN:
            if now - c.opened_at >= self.cfg.cooldown_seconds:
                self._set_state(key, c, HALF_OPEN)
                c.probing = False
            else:
                return False
        if c.state == HALF_OPEN:
            if c.probing:          # only one probe in flight
                return False
            c.probing = True
            return True
        return True                # CLOSED

    def record_success(self, key: str) -> None:
        c = self._get(key)
        c.failures.clear()
        c.probing = False
        if c.state != CLOSED:
            self._set_state(key, c, CLOSED)

    def record_failure(self, key: str) -> None:
        c = self._get(key)
        now = time.time()
        if c.state == HALF_OPEN:   # probe failed → straight back to OPEN
            c.opened_at = now
            c.probing = False
            self._set_state(key, c, OPEN)
            return
        c.failures.append(now)
        cutoff = now - self.cfg.window_seconds
        while c.failures and c.failures[0] < cutoff:
            c.failures.popleft()
        if len(c.failures) >= self.cfg.failure_threshold:
            c.opened_at = now
            self._set_state(key, c, OPEN)

    def state_of(self, key: str) -> str:
        return self._get(key).state

    def snapshot(self) -> dict[str, str]:
        return {k: c.state for k, c in self._circuits.items()}
