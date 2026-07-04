"""Request enrichment: centralized policy applied before forwarding.

Lets the org enforce standard system prompts, compliance disclaimers, and a
basic content filter in one place instead of every team reimplementing them.
"""
from __future__ import annotations

from app.config import TeamConfig
from app.models import ChatCompletionRequest, ChatMessage, GatewayError


def enrich(req: ChatCompletionRequest, team: TeamConfig) -> ChatCompletionRequest:
    e = team.enrichment

    # Content filter: reject requests containing banned phrases (non-retryable).
    if e.banned_phrases:
        joined = " ".join(m.content for m in req.messages).lower()
        for phrase in e.banned_phrases:
            if phrase.lower() in joined:
                raise GatewayError(
                    f"request blocked by content policy (matched: '{phrase}')",
                    status_code=400, retryable=False,
                )

    messages = list(req.messages)
    if e.system_prefix:
        messages.insert(0, ChatMessage(role="system", content=e.system_prefix))
    if e.disclaimer:
        messages.append(ChatMessage(role="system", content=e.disclaimer))

    if e.system_prefix or e.disclaimer:
        req = req.model_copy(update={"messages": messages})
    return req
