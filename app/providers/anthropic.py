"""Anthropic adapter.

Translates the unified (OpenAI-style) request into Anthropic's Messages API:
system prompt is hoisted out of `messages`, and usage/content are mapped back.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app.models import ChatCompletionRequest, GatewayError, ProviderResult, Usage
from app.providers.base import Provider

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    def __init__(self, name: str, base_url: str, api_key: str):
        super().__init__(name)
        self.api_key = api_key
        self.client = httpx.AsyncClient(base_url=base_url, timeout=60.0)

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _body(self, req: ChatCompletionRequest, provider_model: str, stream: bool) -> dict:
        system = "\n".join(m.content for m in req.messages if m.role == "system")
        msgs = [{"role": m.role, "content": m.content}
                for m in req.messages if m.role in ("user", "assistant")]
        body: dict = {
            "model": provider_model,
            "messages": msgs,
            "max_tokens": req.max_tokens or 1024,   # required by Anthropic
            "stream": stream,
        }
        if system:
            body["system"] = system
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None:
            body["top_p"] = req.top_p
        body.update(req.extra)
        return body

    async def chat(self, req: ChatCompletionRequest, provider_model: str) -> ProviderResult:
        if not self.api_key:
            raise GatewayError("ANTHROPIC_API_KEY not set", 401, retryable=False)
        try:
            r = await self.client.post("/messages", headers=self._headers(),
                                       json=self._body(req, provider_model, False))
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise GatewayError(f"anthropic {status}: {e.response.text[:200]}",
                               status, retryable=status == 429 or status >= 500)
        except httpx.TimeoutException:
            raise GatewayError("anthropic timeout", 504, retryable=True)
        data = r.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        u = data.get("usage", {})
        inp, out = u.get("input_tokens", 0), u.get("output_tokens", 0)
        return ProviderResult(
            content=text,
            usage=Usage(prompt_tokens=inp, completion_tokens=out, total_tokens=inp + out),
            finish_reason=data.get("stop_reason", "stop"),
            provider_model=provider_model,
        )

    async def stream(self, req: ChatCompletionRequest, provider_model: str) -> AsyncIterator[str]:
        if not self.api_key:
            raise GatewayError("ANTHROPIC_API_KEY not set", 401, retryable=False)
        async with self.client.stream("POST", "/messages", headers=self._headers(),
                                      json=self._body(req, provider_model, True)) as r:
            if r.status_code >= 400:
                text = (await r.aread()).decode()[:200]
                raise GatewayError(f"anthropic {r.status_code}: {text}", r.status_code,
                                   retryable=r.status_code == 429 or r.status_code >= 500)
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {}).get("text")
                    if delta:
                        yield delta

    async def aclose(self) -> None:
        await self.client.aclose()
