"""Routing + resilience: circuit breaker → retry w/ backoff → fallback chain.

For a requested model we build an ordered list of candidates (the model itself,
then its tier's fallback chain). For each candidate we:
  1. skip it if its circuit is OPEN,
  2. retry the call with exponential backoff on *retryable* errors,
  3. on a non-retryable error (auth, content policy) fail fast — no fallback,
  4. on exhausted retries, trip the circuit and move to the next candidate.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import AsyncIterator

from app.circuit_breaker import CircuitBreaker
from app.config import GatewayConfig
from app.metrics import ERRORS, FALLBACKS
from app.models import ChatCompletionRequest, GatewayError, ProviderResult
from app.providers.base import Provider

logger = logging.getLogger("gateway.router")


class Router:
    def __init__(self, config: GatewayConfig, providers: dict[str, Provider],
                 breaker: CircuitBreaker):
        self.config = config
        self.providers = providers
        self.breaker = breaker

    def refresh(self, config: GatewayConfig, providers: dict[str, Provider]) -> None:
        """Called after a hot config reload."""
        self.config = config
        self.providers = providers

    def _provider_for(self, model: str) -> tuple[Provider, str]:
        mc = self.config.models[model]
        provider = self.providers[mc.provider]
        return provider, (mc.provider_model or model)

    async def _backoff(self, attempt: int) -> None:
        r = self.config.resilience.retry
        delay = min(r.max_delay_ms, r.base_delay_ms * (2 ** (attempt - 1)))
        delay = delay * (0.5 + random.random() / 2)  # jitter
        await asyncio.sleep(delay / 1000)

    async def _try_model(self, model: str, req: ChatCompletionRequest) -> ProviderResult:
        """Call one model with retries. Raises the last error if all attempts fail."""
        provider, provider_model = self._provider_for(model)
        last_err: GatewayError | None = None
        for attempt in range(1, self.config.resilience.retry.max_attempts + 1):
            try:
                return await provider.chat(req, provider_model)
            except GatewayError as e:
                last_err = e
                ERRORS.labels(team="_", provider=provider.name,
                              error_type=type(e).__name__).inc()
                if not e.retryable:
                    raise                       # auth / content policy → no retry, no fallback
                if attempt < self.config.resilience.retry.max_attempts:
                    logger.info("retry %d/%d model=%s: %s", attempt,
                                self.config.resilience.retry.max_attempts, model, e.message)
                    await self._backoff(attempt)
        raise last_err or GatewayError(f"model {model} failed", 502, retryable=True)

    async def route_chat(self, req: ChatCompletionRequest) -> tuple[ProviderResult, dict]:
        chain = self.config.resolve_chain(req.model)
        if not chain:
            raise GatewayError(f"unknown model: {req.model}", 400, retryable=False)

        meta = {"requested_model": req.model, "attempted": [], "fallback_used": False}
        last_err: GatewayError | None = None
        for idx, model in enumerate(chain):
            if not self.breaker.allow(model):
                meta["attempted"].append({"model": model, "skipped": "circuit_open"})
                continue
            try:
                result = await self._try_model(model, req)
            except GatewayError as e:
                last_err = e
                self.breaker.record_failure(model)
                meta["attempted"].append({"model": model, "error": e.message})
                if not e.retryable:
                    raise                       # don't fall back on non-retryable errors
                continue                        # fall back to next candidate
            self.breaker.record_success(model)
            meta["attempted"].append({"model": model, "ok": True})
            meta["served_model"] = model
            if idx > 0:
                meta["fallback_used"] = True
                FALLBACKS.labels(from_model=req.model, to_model=model).inc()
            return result, meta

        raise last_err or GatewayError("all providers unavailable", 503, retryable=True)

    async def route_stream(self, req: ChatCompletionRequest,
                           meta: dict) -> AsyncIterator[str]:
        """Stream with fallback *before first byte*. Once bytes flow we commit."""
        chain = self.config.resolve_chain(req.model)
        if not chain:
            raise GatewayError(f"unknown model: {req.model}", 400, retryable=False)
        meta.setdefault("requested_model", req.model)
        meta.setdefault("attempted", [])
        last_err: GatewayError | None = None

        for idx, model in enumerate(chain):
            if not self.breaker.allow(model):
                meta["attempted"].append({"model": model, "skipped": "circuit_open"})
                continue
            provider, provider_model = self._provider_for(model)
            try:
                gen = provider.stream(req, provider_model)
                first = await gen.__anext__()   # force connection; may raise before any bytes
            except StopAsyncIteration:
                first = None
                gen = None
            except GatewayError as e:
                last_err = e
                self.breaker.record_failure(model)
                meta["attempted"].append({"model": model, "error": e.message})
                if not e.retryable:
                    raise
                continue
            # Committed to this model.
            self.breaker.record_success(model)
            meta["served_model"] = model
            if idx > 0:
                meta["fallback_used"] = True
                FALLBACKS.labels(from_model=req.model, to_model=model).inc()
            if first is not None:
                yield first
            if gen is not None:
                async for chunk in gen:
                    yield chunk
            return

        raise last_err or GatewayError("all providers unavailable", 503, retryable=True)
