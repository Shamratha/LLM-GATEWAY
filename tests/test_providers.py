"""Provider adapter contract tests.

Exercises the real translation code (unified request -> provider format ->
normalized ProviderResult) against a mocked HTTP transport, so the
'provider-agnostic' claim is verified without real API keys.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.models import ChatCompletionRequest, ChatMessage
from app.providers.anthropic import AnthropicProvider
from app.providers.ollama import OllamaProvider
from app.providers.openai import OpenAIProvider


def _mock_client(base_url: str, handler):
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


def _req():
    return ChatCompletionRequest(model="m", max_tokens=64, messages=[
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="hi"),
    ])


@pytest.mark.asyncio
async def test_openai_translation_and_normalization():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        })

    p = OpenAIProvider("openai", "https://api.openai.com/v1", "test-key")
    p.client = _mock_client("https://api.openai.com/v1", handler)
    result = await p.chat(_req(), "gpt-4o")

    assert seen["path"].endswith("/chat/completions")
    assert seen["body"]["model"] == "gpt-4o"
    assert result.content == "hi there"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (4, 2)


@pytest.mark.asyncio
async def test_anthropic_hoists_system_and_normalizes_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        })

    p = AnthropicProvider("anthropic", "https://api.anthropic.com/v1", "test-key")
    p.client = _mock_client("https://api.anthropic.com/v1", handler)
    result = await p.chat(_req(), "claude-sonnet-4-6")

    body = seen["body"]
    assert body["system"] == "be terse"                       # system hoisted out of messages
    assert body["max_tokens"] == 64                           # required field passed through
    assert all(m["role"] in ("user", "assistant") for m in body["messages"])
    assert result.content == "ok"
    assert result.usage.total_tokens == 8                     # 5 + 3 normalized


@pytest.mark.asyncio
async def test_ollama_translation_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={
            "message": {"content": "yo"},
            "prompt_eval_count": 7, "eval_count": 4, "done_reason": "stop",
        })

    p = OllamaProvider("ollama", "http://localhost:11434")
    p.client = _mock_client("http://localhost:11434", handler)
    result = await p.chat(_req(), "llama3")

    assert seen["path"].endswith("/api/chat")
    assert result.content == "yo"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (7, 4)


@pytest.mark.asyncio
async def test_provider_maps_http_error_to_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    p = OpenAIProvider("openai", "https://api.openai.com/v1", "test-key")
    p.client = _mock_client("https://api.openai.com/v1", handler)
    from app.models import GatewayError
    with pytest.raises(GatewayError) as ei:
        await p.chat(_req(), "gpt-4o")
    assert ei.value.retryable is True                         # 5xx is retryable → triggers fallback
