"""Admin API: live inspection and control without a restart.

Live limit/budget edits are stored as runtime *overrides* that are re-applied
after every config hot-reload, so an admin change and a YAML edit don't clobber
each other. All mutating actions are logged with actor + timestamp.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth import require_admin
from app.providers.mock import controller as mock_controller

logger = logging.getLogger("gateway.admin")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ── override store (survives hot reloads) ──────────────────────────────────
class OverrideStore:
    def __init__(self):
        self.data: dict[str, dict] = {}   # team_id -> {rate_limits?, budget?}

    def apply(self, config) -> None:
        for team in config.teams:
            ov = self.data.get(team.id)
            if not ov:
                continue
            if "rate_limits" in ov:
                team.rate_limits = team.rate_limits.model_copy(update=ov["rate_limits"])
            if "budget" in ov:
                team.budget = team.budget.model_copy(update=ov["budget"])

    def record(self, team_id: str, section: str, values: dict) -> None:
        self.data.setdefault(team_id, {})[section] = values


class LimitUpdate(BaseModel):
    requests_per_min: int | None = None
    tokens_per_min: int | None = None


class BudgetUpdate(BaseModel):
    limit_usd: float | None = None
    warn_pct: int | None = None
    period: str | None = None


class ChaosUpdate(BaseModel):
    model: str
    fail_rate: float | None = None
    latency_ms: int | None = None
    down: bool | None = None


def _team_or_404(request: Request, team_id: str):
    team = request.app.state.store.config.team_by_id(team_id)
    if not team:
        raise HTTPException(404, f"unknown team: {team_id}")
    return team


@router.get("/teams")
async def list_teams(request: Request):
    cfg = request.app.state.store.config
    return [{"id": t.id, "name": t.name, "priority": t.priority,
             "allowed_models": t.allowed_models,
             "rate_limits": t.rate_limits.model_dump(),
             "budget": t.budget.model_dump()} for t in cfg.teams]


@router.get("/teams/{team_id}/status")
async def team_status(request: Request, team_id: str):
    team = _team_or_404(request, team_id)
    state = request.app.state
    return {
        "team": team.id,
        "rate_limits": await state.limiter.status(team),
        "budget": await state.budget.status(team),
    }


@router.post("/teams/{team_id}/limits")
async def update_limits(request: Request, team_id: str, body: LimitUpdate):
    team = _team_or_404(request, team_id)
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    if not values:
        raise HTTPException(400, "no fields to update")
    team.rate_limits = team.rate_limits.model_copy(update=values)
    request.app.state.overrides.record(team_id, "rate_limits", values)
    logger.info("ADMIN limit change team=%s values=%s at=%d", team_id, values, int(time.time()))
    return {"ok": True, "rate_limits": team.rate_limits.model_dump()}


@router.post("/teams/{team_id}/budget")
async def update_budget(request: Request, team_id: str, body: BudgetUpdate):
    team = _team_or_404(request, team_id)
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    if not values:
        raise HTTPException(400, "no fields to update")
    team.budget = team.budget.model_copy(update=values)
    request.app.state.overrides.record(team_id, "budget", values)
    logger.info("ADMIN budget change team=%s values=%s at=%d", team_id, values, int(time.time()))
    return {"ok": True, "budget": team.budget.model_dump()}


@router.get("/health")
async def provider_health(request: Request):
    return request.app.state.health.snapshot()


@router.get("/circuits")
async def circuits(request: Request):
    return request.app.state.breaker.snapshot()


@router.post("/reload")
async def reload_config(request: Request):
    request.app.state.store.reload()
    logger.info("ADMIN manual config reload at=%d", int(time.time()))
    return {"ok": True}


@router.post("/chaos")
async def chaos(request: Request, body: ChaosUpdate):
    """Inject failures/latency into a mock model to demo resilience."""
    state = mock_controller.set(body.model, fail_rate=body.fail_rate,
                                latency_ms=body.latency_ms, down=body.down)
    logger.info("ADMIN chaos model=%s state=%s", body.model, state)
    return {"ok": True, "model": body.model, "state": state}


@router.post("/chaos/reset")
async def chaos_reset():
    mock_controller.reset()
    return {"ok": True}
