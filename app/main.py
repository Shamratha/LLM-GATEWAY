"""FastAPI app + the request pipeline.

Pipeline per request:
  auth → model authorization → enrichment → rate limit (RPM) →
  rate limit (TPM) → budget check → route (retry/fallback) →
  charge budget + record metrics → respond.

Public surface is OpenAI-compatible (POST /v1/chat/completions), so existing
OpenAI SDKs work by pointing base_url at the gateway.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.admin import OverrideStore, router as admin_router
from app.auth import authenticate_team
from app.budget import BudgetManager, compute_cost
from app.circuit_breaker import CircuitBreaker
from app.config import ConfigStore, TeamConfig, settings
from app.enrichment import enrich
from app.health import HealthMonitor
from app.metrics import (BUDGET_REJECTS, COST, LATENCY, OVERHEAD, RATELIMIT_REJECTS,
                         REQUESTS, TOKENS)
from app.models import (ChatCompletionRequest, ChatCompletionResponse, Choice,
                        ChatMessage, GatewayError, Usage)
from app.providers import build_registry
from app.providers.base import estimate_tokens, messages_to_text
from app.rate_limiter import RateLimiter
from app.router import Router

logging.basicConfig(level=settings.log_level,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = ConfigStore(settings.config_path)
    cfg = store.config
    r = redis.from_url(settings.redis_url, decode_responses=True)
    providers = build_registry(cfg)
    breaker = CircuitBreaker(cfg.resilience.circuit_breaker)
    router_ = Router(cfg, providers, breaker)
    health = HealthMonitor(cfg, providers)
    overrides = OverrideStore()

    app.state.store = store
    app.state.redis = r
    app.state.providers = providers
    app.state.breaker = breaker
    app.state.router = router_
    app.state.health = health
    app.state.overrides = overrides
    app.state.limiter = RateLimiter(r)
    app.state.budget = BudgetManager(r)

    # On hot reload: rebuild providers, re-apply admin overrides, refresh deps.
    def _on_reload(new_cfg):
        overrides.apply(new_cfg)
        new_providers = build_registry(new_cfg)
        app.state.providers = new_providers
        router_.refresh(new_cfg, new_providers)
        health.refresh(new_cfg, new_providers)
    store.on_reload(_on_reload)

    store.start()
    health.start()
    logger.info("gateway up: %d teams, %d models", len(cfg.teams), len(cfg.models))
    try:
        yield
    finally:
        await store.stop()
        await health.stop()
        for p in app.state.providers.values():
            await p.aclose()
        await r.aclose()


app = FastAPI(title="LLM Gateway", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)

from app.tracing import setup_tracing  # noqa: E402
setup_tracing(app)


@app.exception_handler(GatewayError)
async def _gateway_error_handler(request: Request, exc: GatewayError):
    headers = {}
    if getattr(exc, "retry_after", None):
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"message": exc.message, "type": type(exc).__name__}},
                        headers=headers)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/models")
async def list_models(request: Request, team: TeamConfig = Depends(authenticate_team)):
    cfg = request.app.state.store.config
    allowed = team.allowed_models or list(cfg.models)
    return {"object": "list",
            "data": [{"id": m, "object": "model", "tier": cfg.models[m].tier}
                     for m in allowed if m in cfg.models]}


def _authorize_model(request: Request, team: TeamConfig, model: str) -> None:
    cfg = request.app.state.store.config
    if model not in cfg.models:
        raise GatewayError(f"unknown model: {model}", 400, retryable=False)
    if team.allowed_models and model not in team.allowed_models:
        raise GatewayError(f"team '{team.id}' is not allowed to use model '{model}'",
                           403, retryable=False)


async def _enforce_limits(request: Request, team: TeamConfig, est_input: int) -> None:
    limiter: RateLimiter = request.app.state.limiter
    budget: BudgetManager = request.app.state.budget

    allowed, retry_after = await limiter.check_request(team)
    if not allowed:
        RATELIMIT_REJECTS.labels(team=team.id, reason="rpm").inc()
        from app.models import RateLimitError
        raise RateLimitError(f"request rate limit exceeded for team '{team.id}'", retry_after)

    allowed, retry_after = await limiter.check_tokens(team, est_input)
    if not allowed:
        await limiter.refund_request(team)   # don't penalize RPM for a TPM rejection
        RATELIMIT_REJECTS.labels(team=team.id, reason="tpm").inc()
        from app.models import RateLimitError
        raise RateLimitError(f"token rate limit exceeded for team '{team.id}'", retry_after)

    ok, spent, limit = await budget.check(team)
    if not ok:
        BUDGET_REJECTS.labels(team=team.id).inc()
        from app.models import BudgetExceededError
        raise BudgetExceededError(
            f"budget cap reached for team '{team.id}': ${spent:.2f} / ${limit:.2f} "
            f"({team.budget.period}). Requests are blocked until the period resets.")


async def _finalize(request: Request, team: TeamConfig, served_model: str,
                    usage: Usage, est_input: int) -> float:
    """Charge budget, reconcile TPM for actual usage, record metrics."""
    cfg = request.app.state.store.config
    mc = cfg.models[served_model]
    cost = compute_cost(mc, usage)
    await request.app.state.budget.charge(team, cost)
    # Reconcile TPM bucket: we reserved est_input, actual is total.
    extra = usage.total_tokens - est_input
    if extra > 0:
        await request.app.state.limiter.check_tokens(team, extra)
    TOKENS.labels(team=team.id, model=served_model, direction="input").inc(usage.prompt_tokens)
    TOKENS.labels(team=team.id, model=served_model, direction="output").inc(usage.completion_tokens)
    COST.labels(team=team.id, model=served_model).inc(cost)
    return cost


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest,
                           team: TeamConfig = Depends(authenticate_team)):
    t0 = time.perf_counter()
    _authorize_model(request, team, body.model)
    body = enrich(body, team)

    est_input = estimate_tokens(messages_to_text(body))
    await _enforce_limits(request, team, est_input)

    router_: Router = request.app.state.router

    if body.stream:
        return await _stream_response(request, team, body, est_input, t0)

    t_provider = time.perf_counter()
    result, meta = await router_.route_chat(body)
    provider_time = time.perf_counter() - t_provider
    served_model = meta.get("served_model", body.model)

    cost = await _finalize(request, team, served_model, result.usage, est_input)

    provider_name = request.app.state.store.config.models[served_model].provider
    total = time.perf_counter() - t0
    LATENCY.labels(provider=provider_name).observe(total)
    OVERHEAD.observe(max(0.0, total - provider_time))
    REQUESTS.labels(team=team.id, model=served_model, provider=provider_name, status="ok").inc()

    _annotate_span(team.id, body.model, served_model, result.usage, cost,
                   meta.get("fallback_used", False))

    resp = ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
        model=served_model,
        choices=[Choice(message=ChatMessage(role="assistant", content=result.content),
                        finish_reason=result.finish_reason)],
        usage=result.usage,
        gateway={**meta, "cost_usd": round(cost, 6),
                 "overhead_ms": round(max(0.0, total - provider_time) * 1000, 2)},
    )
    return resp


def _annotate_span(team_id: str, requested: str, served: str, usage: Usage,
                   cost: float, fallback_used: bool) -> None:
    """Attach LLM attributes to the current OTel span (no-op if tracing off)."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        span.set_attribute("gateway.team_id", team_id)
        span.set_attribute("gateway.model_requested", requested)
        span.set_attribute("gateway.model_served", served)
        span.set_attribute("gateway.prompt_tokens", usage.prompt_tokens)
        span.set_attribute("gateway.completion_tokens", usage.completion_tokens)
        span.set_attribute("gateway.cost_usd", cost)
        span.set_attribute("gateway.fallback_used", fallback_used)
    except Exception:
        pass


def _sse_chunk(chunk_id: str, model: str, delta: dict, finish: str | None = None) -> str:
    payload = {
        "id": chunk_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_response(request: Request, team: TeamConfig,
                           body: ChatCompletionRequest, est_input: int, t0: float):
    router_: Router = request.app.state.router
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    meta: dict = {}

    async def gen():
        collected: list[str] = []
        served_model = body.model
        try:
            first = True
            async for delta in router_.route_stream(body, meta):
                if first:
                    served_model = meta.get("served_model", body.model)
                    yield _sse_chunk(chunk_id, served_model, {"role": "assistant"})
                    first = False
                collected.append(delta)
                yield _sse_chunk(chunk_id, served_model, {"content": delta})
            yield _sse_chunk(chunk_id, served_model, {}, finish="stop")
            yield "data: [DONE]\n\n"
        except GatewayError as e:
            yield f"data: {json.dumps({'error': {'message': e.message}})}\n\n"
            return
        finally:
            # Log/charge the full response for observability even on streaming.
            content = "".join(collected)
            if content:
                usage = Usage(prompt_tokens=est_input,
                              completion_tokens=estimate_tokens(content),
                              total_tokens=est_input + estimate_tokens(content))
                served_model = meta.get("served_model", body.model)
                cost = await _finalize(request, team, served_model, usage, est_input)
                provider_name = request.app.state.store.config.models[served_model].provider
                LATENCY.labels(provider=provider_name).observe(time.perf_counter() - t0)
                REQUESTS.labels(team=team.id, model=served_model,
                                provider=provider_name, status="ok").inc()

    return StreamingResponse(gen(), media_type="text/event-stream")
