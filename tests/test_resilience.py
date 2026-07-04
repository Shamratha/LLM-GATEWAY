"""Unit tests for the circuit breaker and router fallback logic."""
from __future__ import annotations

import pytest

from app.circuit_breaker import CircuitBreaker, CLOSED, HALF_OPEN, OPEN
from app.config import CircuitBreakerConfig, GatewayConfig, ModelConfig, Pricing
from app.models import ChatCompletionRequest, ChatMessage, GatewayError, ProviderResult, Usage
from app.providers.base import Provider
from app.router import Router


def test_circuit_opens_after_threshold():
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, window_seconds=30, cooldown_seconds=1))
    assert cb.allow("m")
    for _ in range(3):
        cb.record_failure("m")
    assert cb.state_of("m") == OPEN
    assert cb.allow("m") is False


def test_circuit_success_resets():
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, window_seconds=30, cooldown_seconds=1))
    cb.record_failure("m")
    cb.record_success("m")
    assert cb.state_of("m") == CLOSED


class _ScriptedProvider(Provider):
    """Fails a fixed number of times, then succeeds."""
    def __init__(self, name, fail_times=0, retryable=True):
        super().__init__(name)
        self.fail_times = fail_times
        self.retryable = retryable
        self.calls = 0

    async def chat(self, req, provider_model):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise GatewayError("boom", 503, retryable=self.retryable)
        return ProviderResult(content="ok", usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                              provider_model=provider_model)

    def stream(self, req, provider_model):
        raise NotImplementedError


def _config():
    return GatewayConfig(
        models={
            "primary": ModelConfig(provider="p1", tier="frontier", pricing=Pricing()),
            "backup": ModelConfig(provider="p2", tier="frontier", pricing=Pricing()),
        },
        fallback_chains={"frontier": ["primary", "backup"]},
    )


def _req(model="primary"):
    return ChatCompletionRequest(model=model, messages=[ChatMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_retries_then_succeeds_same_model():
    cfg = _config()
    providers = {"p1": _ScriptedProvider("p1", fail_times=2), "p2": _ScriptedProvider("p2")}
    router = Router(cfg, providers, CircuitBreaker(cfg.resilience.circuit_breaker))
    result, meta = await router.route_chat(_req())
    assert result.content == "ok"
    assert meta["served_model"] == "primary"
    assert meta["fallback_used"] is False
    assert providers["p1"].calls == 3   # 2 failures + 1 success


@pytest.mark.asyncio
async def test_falls_back_when_primary_exhausted():
    cfg = _config()
    providers = {"p1": _ScriptedProvider("p1", fail_times=99), "p2": _ScriptedProvider("p2")}
    router = Router(cfg, providers, CircuitBreaker(cfg.resilience.circuit_breaker))
    result, meta = await router.route_chat(_req())
    assert result.content == "ok"
    assert meta["served_model"] == "backup"
    assert meta["fallback_used"] is True


@pytest.mark.asyncio
async def test_non_retryable_does_not_fall_back():
    cfg = _config()
    providers = {"p1": _ScriptedProvider("p1", fail_times=1, retryable=False),
                 "p2": _ScriptedProvider("p2")}
    router = Router(cfg, providers, CircuitBreaker(cfg.resilience.circuit_breaker))
    with pytest.raises(GatewayError):
        await router.route_chat(_req())
    assert providers["p2"].calls == 0   # never fell back
