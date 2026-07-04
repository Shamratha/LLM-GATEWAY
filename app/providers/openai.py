"""OpenAI adapter. The public API mirrors OpenAI, so translation is minimal."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from app.models import ChatCompletionRequest, GatewayError, ProviderResult, Usage
from app.providers.base import Provider, estimate_tokens, messages_to_text


def _map_http_error(e: httpx.HTTPStatusError) -> GatewayError:
    status = e.response.status_code
    retryable = status == 429 or status >= 500
    return GatewayError(f"openai {status}: {e.response.text[:200]}", status, retryable)


class OpenAIProvider(Provider):
    def __init__(self, name: str, base_url: str, api_key: str):
        super().__init__(name)
        self.api_key = api_key
        self.client = httpx.AsyncClient(base_url=base_url, timeout=60.0)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _body(self, req: ChatCompletionRequest, provider_model: str, stream: bool) -> dict:
        body = {
            "model": provider_model,
            "messages": [m.model_dump() for m in req.messages],
            "stream": stream,
        }
        for k in ("temperature", "max_tokens", "top_p", "stop"):
            v = getattr(req, k)
            if v is not None:
                body[k] = v
        body.update(req.extra)
        return body

    async def chat(self, req: ChatCompletionRequest, provider_model: str) -> ProviderResult:
        if not self.api_key:
            raise GatewayError("OPENAI_API_KEY not set", 401, retryable=False)
        try:
            r = await self.client.post("/chat/completions", headers=self._headers(),
                                       json=self._body(req, provider_model, False))
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise _map_http_error(e)
        except httpx.TimeoutException:
            raise GatewayError("openai timeout", 504, retryable=True)
        data = r.json()
        choice = data["choices"][0]["message"]["content"]
        u = data.get("usage", {})
        return ProviderResult(
            content=choice,
            usage=Usage(prompt_tokens=u.get("prompt_tokens", 0),
                        completion_tokens=u.get("completion_tokens", 0),
                        total_tokens=u.get("total_tokens", 0)),
            finish_reason=data["choices"][0].get("finish_reason", "stop"),
            provider_model=provider_model,
        )

    async def stream(self, req: ChatCompletionRequest, provider_model: str) -> AsyncIterator[str]:
        if not self.api_key:
            raise GatewayError("OPENAI_API_KEY not set", 401, retryable=False)
        async with self.client.stream("POST", "/chat/completions", headers=self._headers(),
                                      json=self._body(req, provider_model, True)) as r:
            if r.status_code >= 400:
                text = (await r.aread()).decode()[:200]
                raise GatewayError(f"openai {r.status_code}: {text}", r.status_code,
                                   retryable=r.status_code == 429 or r.status_code >= 500)
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    yield delta

    async def aclose(self) -> None:
        await self.client.aclose()
