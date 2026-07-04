"""API-key auth for team requests and the admin API.

Key comparisons use hmac.compare_digest (constant-time) so an attacker can't
recover a valid key byte-by-byte via response-timing differences. The team
lookup scans all teams without short-circuiting on the first mismatch.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request

from app.config import GatewayConfig, TeamConfig, settings


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    return value[7:].strip() if value.lower().startswith("bearer ") else value.strip()


def _find_team_constant_time(config: GatewayConfig, key: str) -> TeamConfig | None:
    """Constant-time-ish team lookup: compare against every team's key."""
    match: TeamConfig | None = None
    for team in config.teams:
        if hmac.compare_digest(team.api_key, key):
            match = team
    return match


async def authenticate_team(
    request: Request,
    authorization: str | None = Header(default=None),
) -> TeamConfig:
    """Resolve the caller's team from its API key (Authorization: Bearer <key>)."""
    key = _bearer(authorization)
    if not key:
        raise HTTPException(status_code=401, detail="missing API key")
    config = request.app.state.store.config
    team = _find_team_constant_time(config, key)
    if not team:
        raise HTTPException(status_code=401, detail="invalid API key")
    return team


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    key = _bearer(authorization)
    if not key or not hmac.compare_digest(key, settings.admin_api_key):
        raise HTTPException(status_code=403, detail="admin credentials required")
