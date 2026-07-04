"""Background provider health monitoring.

Periodically probes every configured model and tracks a rolling
healthy/degraded/down status + latency, exported to Prometheus and surfaced by
the Admin API. Runs independently of the circuit breaker (which reacts to real
traffic); this gives proactive visibility and post-incident history.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from app.config import GatewayConfig
from app.metrics import PROVIDER_HEALTH
from app.providers.base import Provider

logger = logging.getLogger("gateway.health")

HEALTHY, DEGRADED, DOWN = "healthy", "degraded", "down"
_CODE = {HEALTHY: 1.0, DEGRADED: 0.5, DOWN: 0.0}


class HealthMonitor:
    def __init__(self, config: GatewayConfig, providers: dict[str, Provider]):
        self.config = config
        self.providers = providers
        self.status: dict[str, dict] = {}
        self.history: dict[str, deque] = {}
        self._task: asyncio.Task | None = None

    def refresh(self, config: GatewayConfig, providers: dict[str, Provider]) -> None:
        self.config = config
        self.providers = providers

    async def _probe(self, model: str) -> None:
        mc = self.config.models[model]
        provider = self.providers.get(mc.provider)
        if not provider:
            return
        provider_model = mc.provider_model or model
        start = time.time()
        try:
            ok = await asyncio.wait_for(
                provider.health_check(provider_model),
                timeout=self.config.resilience.health_check.timeout_seconds,
            )
            latency_ms = (time.time() - start) * 1000
            if not ok:
                state = DOWN
            elif latency_ms > 2000:
                state = DEGRADED
            else:
                state = HEALTHY
        except (asyncio.TimeoutError, Exception):
            latency_ms = (time.time() - start) * 1000
            state = DOWN

        self.status[model] = {
            "status": state,
            "latency_ms": round(latency_ms, 1),
            "checked_at": time.time(),
        }
        h = self.history.setdefault(model, deque(maxlen=120))
        h.append({"t": time.time(), "status": state, "latency_ms": round(latency_ms, 1)})
        PROVIDER_HEALTH.labels(provider_model=model).set(_CODE[state])

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.gather(*(self._probe(m) for m in self.config.models))
            except Exception as e:
                logger.error("health probe error: %s", e)
            await asyncio.sleep(self.config.resilience.health_check.interval_seconds)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def snapshot(self) -> dict:
        return dict(self.status)
