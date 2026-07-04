"""In-process mock provider.

No network, no keys, no cost — but it can simulate latency and failures so you
can demo retries, fallback, and circuit breakers. Chaos is controlled at
runtime (via the Admin API), keyed by provider_model.
"""
from __future__ import annotations

import asyncio
import random
from typing import AsyncIterator

from app.models import ChatCompletionRequest, GatewayError, ProviderResult, Usage
from app.providers.base import Provider, estimate_tokens, messages_to_text


class MockController:
    """Runtime knobs for chaos engineering the mock provider."""

    def __init__(self):
        # per provider_model: {"fail_rate": float, "latency_ms": int, "down": bool}
        self._state: dict[str, dict] = {}

    def get(self, model: str) -> dict:
        return self._state.get(model, {"fail_rate": 0.0, "latency_ms": 40, "down": False})

    def set(self, model: str, *, fail_rate: float | None = None,
            latency_ms: int | None = None, down: bool | None = None) -> dict:
        s = dict(self.get(model))
        if fail_rate is not None:
            s["fail_rate"] = max(0.0, min(1.0, fail_rate))
        if latency_ms is not None:
            s["latency_ms"] = max(0, latency_ms)
        if down is not None:
            s["down"] = down
        self._state[model] = s
        return s

    def reset(self) -> None:
        self._state.clear()

    def snapshot(self) -> dict:
        return dict(self._state)


# Shared across the process so the Admin API and the provider see the same state.
controller = MockController()


class MockProvider(Provider):
    async def _simulate(self, provider_model: str) -> None:
        s = controller.get(provider_model)
        if s["latency_ms"]:
            await asyncio.sleep(s["latency_ms"] / 1000)
        if s["down"]:
            raise GatewayError(f"mock model '{provider_model}' is down", 503, retryable=True)
        if s["fail_rate"] and random.random() < s["fail_rate"]:
            raise GatewayError(f"mock transient error on '{provider_model}'", 503, retryable=True)

    def _answer(self, req: ChatCompletionRequest, provider_model: str) -> str:
        last = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        return f"[{provider_model}] You said: {last[:200]}"

    async def chat(self, req: ChatCompletionRequest, provider_model: str) -> ProviderResult:
        await self._simulate(provider_model)
        answer = self._answer(req, provider_model)
        return ProviderResult(
            content=answer,
            usage=Usage(
                prompt_tokens=estimate_tokens(messages_to_text(req)),
                completion_tokens=estimate_tokens(answer),
                total_tokens=estimate_tokens(messages_to_text(req)) + estimate_tokens(answer),
            ),
            finish_reason="stop",
            provider_model=provider_model,
        )

    async def stream(self, req: ChatCompletionRequest, provider_model: str) -> AsyncIterator[str]:
        await self._simulate(provider_model)
        answer = self._answer(req, provider_model)
        for word in answer.split(" "):
            await asyncio.sleep(0.02)
            yield word + " "

    async def health_check(self, provider_model: str) -> bool:
        return not controller.get(provider_model)["down"]
