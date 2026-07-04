"""Provider interface + shared helpers.

Every adapter normalizes to the same ProviderResult so the router is
provider-agnostic. Adapters raise GatewayError (with retryable set) so the
resilience layer can decide whether to retry/fall back.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.models import ChatCompletionRequest, ProviderResult


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for providers that don't report usage."""
    return max(1, len(text) // 4)


def messages_to_text(req: ChatCompletionRequest) -> str:
    return "\n".join(f"{m.role}: {m.content}" for m in req.messages)


class Provider(ABC):
    """Abstract upstream. `provider_model` is the concrete model id to call."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def chat(self, req: ChatCompletionRequest, provider_model: str) -> ProviderResult:
        ...

    @abstractmethod
    def stream(self, req: ChatCompletionRequest, provider_model: str) -> AsyncIterator[str]:
        """Yield content chunks (text deltas). Implemented as an async generator."""
        ...

    async def health_check(self, provider_model: str) -> bool:
        """Lightweight liveness probe. Default: try a tiny chat and see if it works."""
        try:
            from app.models import ChatMessage
            probe = ChatCompletionRequest(
                model=provider_model,
                messages=[ChatMessage(role="user", content="ping")],
                max_tokens=1,
            )
            await self.chat(probe, provider_model)
            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        pass
