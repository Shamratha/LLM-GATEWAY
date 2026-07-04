"""Per-team spend tracking and budget caps.

Spend accumulates in Redis under a period-scoped key (monthly/daily). We
pre-check before serving (block if already over) and charge the real cost
after. Crossing the warn threshold emits a one-time warning per period.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import redis.asyncio as redis

from app.config import ModelConfig, TeamConfig
from app.models import Usage

logger = logging.getLogger("gateway.budget")


def compute_cost(model_cfg: ModelConfig, usage: Usage) -> float:
    """USD cost from token usage and per-1M pricing."""
    return (usage.prompt_tokens / 1_000_000 * model_cfg.pricing.input
            + usage.completion_tokens / 1_000_000 * model_cfg.pricing.output)


def _period_key(team: TeamConfig) -> str:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m") if team.budget.period == "monthly" else now.strftime("%Y-%m-%d")
    return f"budget:{team.id}:{team.budget.period}:{stamp}"


class BudgetManager:
    def __init__(self, client: redis.Redis):
        self.r = client

    async def _spent(self, key: str) -> float:
        try:
            v = await self.r.get(key)
            return float(v) if v else 0.0
        except redis.RedisError as e:
            logger.warning("redis unavailable for budget read, allowing: %s", e)
            return 0.0

    async def check(self, team: TeamConfig) -> tuple[bool, float, float]:
        """Returns (allowed, spent, limit). Blocks when spend >= limit."""
        spent = await self._spent(_period_key(team))
        limit = team.budget.limit_usd
        return spent < limit, spent, limit

    async def charge(self, team: TeamConfig, cost: float) -> float:
        """Add cost to the period bucket; warn once when crossing warn_pct."""
        key = _period_key(team)
        try:
            new_total = await self.r.incrbyfloat(key, cost)
            # expire the key ~2 periods out so old buckets self-clean
            await self.r.expire(key, 60 * 60 * 24 * 40)
        except redis.RedisError as e:
            logger.warning("redis unavailable for budget charge: %s", e)
            return 0.0
        limit = team.budget.limit_usd
        warn_at = limit * team.budget.warn_pct / 100
        if new_total >= warn_at and (new_total - cost) < warn_at:
            logger.warning("BUDGET WARNING team=%s at %.1f%% ($%.2f / $%.2f)",
                           team.id, new_total / limit * 100, new_total, limit)
        return new_total

    async def status(self, team: TeamConfig) -> dict:
        spent = await self._spent(_period_key(team))
        limit = team.budget.limit_usd
        return {
            "period": team.budget.period,
            "spent_usd": round(spent, 4),
            "limit_usd": limit,
            "utilization_pct": round(spent / limit * 100, 1) if limit else 0.0,
            "warn_pct": team.budget.warn_pct,
        }
