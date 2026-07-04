"""Unified request/response schemas.

The gateway's public API is OpenAI-compatible: clients speak the OpenAI
Chat Completions format and the gateway translates to/from each provider.
This means any existing OpenAI SDK works by just changing the base_url.
"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Public (client-facing) schema — OpenAI Chat Completions ────────────────
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | str | None = None
    # Passthrough for provider-specific extras we don't model explicitly.
    extra: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str                    # the model actually served (may differ after fallback)
    choices: list[Choice]
    usage: Usage
    # Gateway metadata — which provider/model really handled it, whether we fell back.
    gateway: dict[str, Any] = Field(default_factory=dict)


# ── Internal provider result ───────────────────────────────────────────────
class ProviderResult(BaseModel):
    """Normalized result returned by every provider adapter."""
    content: str
    usage: Usage
    finish_reason: str | None = "stop"
    provider_model: str


class GatewayError(Exception):
    """Base error carrying an HTTP status and whether a retry/fallback makes sense."""

    def __init__(self, message: str, status_code: int = 502, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


class RateLimitError(GatewayError):
    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message, status_code=429, retryable=False)
        self.retry_after = retry_after


class BudgetExceededError(GatewayError):
    def __init__(self, message: str):
        super().__init__(message, status_code=402, retryable=False)
