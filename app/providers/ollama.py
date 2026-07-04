"""Ollama adapter (local models). Uses the /api/chat endpoint."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app.models import ChatCompletionRequest, GatewayError, ProviderResult, Usage
from app.providers.base import Provider, estimate_tokens, messages_to_text


class OllamaProvider(Provider):
    def __init__(self, name: str, base_url: str):
        super().__init__(name)
        self.client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    def _body(self, req: ChatCompletionRequest, provider_model: str, stream: bool) -> dict:
        options = {}
        if req.temperature is not None:
            options["temperature"] = req.temperature
        if req.top_p is not None:
            options["top_p"] = req.top_p
        body = {
            "model": provider_model,
            "messages": [m.model_dump() for m in req.messages],
            "stream": stream,
        }
        if options:
            body["options"] = options
        return body

    async def chat(self, req: ChatCompletionRequest, provider_model: str) -> ProviderResult:
        try:
            r = await self.client.post("/api/chat", json=self._body(req, provider_model, False))
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise GatewayError(f"ollama {status}: {e.response.text[:200]}",
                               status, retryable=status >= 500)
        except httpx.TimeoutException:
            raise GatewayError("ollama timeout", 504, retryable=True)
        except httpx.ConnectError:
            raise GatewayError("ollama not reachable", 503, retryable=True)
        data = r.json()
        content = data.get("message", {}).get("content", "")
        # Ollama reports token counts when available; fall back to estimates.
        inp = data.get("prompt_eval_count") or estimate_tokens(messages_to_text(req))
        out = data.get("eval_count") or estimate_tokens(content)
        return ProviderResult(
            content=content,
            usage=Usage(prompt_tokens=inp, completion_tokens=out, total_tokens=inp + out),
            finish_reason=data.get("done_reason", "stop"),
            provider_model=provider_model,
        )

    async def stream(self, req: ChatCompletionRequest, provider_model: str) -> AsyncIterator[str]:
        try:
            async with self.client.stream("POST", "/api/chat",
                                          json=self._body(req, provider_model, True)) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode()[:200]
                    raise GatewayError(f"ollama {r.status_code}: {text}", r.status_code,
                                       retryable=r.status_code >= 500)
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("message", {}).get("content")
                    if delta:
                        yield delta
        except httpx.ConnectError:
            raise GatewayError("ollama not reachable", 503, retryable=True)

    async def aclose(self) -> None:
        await self.client.aclose()
