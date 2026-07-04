"""Distributed token-bucket rate limiting on Redis.

Two buckets per team — requests/min (RPM) and tokens/min (TPM). Enforcement is
atomic via a Lua script (check-and-consume in one round trip), so it stays
correct across many gateway replicas. Falls open (allows) if Redis is down, so
an observability dependency never takes down the data plane.
"""
from __future__ import annotations

import logging
import time

import redis.asyncio as redis

from app.config import TeamConfig

logger = logging.getLogger("gateway.ratelimit")

# Refill-style token bucket with a priority reserve. `reserve` is a floor the
# caller must stay above: a request is allowed only if it leaves >= reserve
# tokens behind. Higher-priority callers pass reserve=0 (can drain the bucket);
# lower-priority callers pass a positive reserve, so they are refused first once
# the bucket runs low — i.e. low-priority traffic is shed under pressure while
# capacity is held back for high-priority traffic. Returns {allowed, remaining, retry_after}.
_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])        -- tokens per second
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])         -- seconds (float)
local requested = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local reserve = tonumber(ARGV[6])     -- floor to leave behind (priority headroom)

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end

-- refill based on elapsed time
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0
if tokens >= requested + reserve then
  allowed = 1
  tokens = tokens - requested
else
  local deficit = (requested + reserve) - tokens
  retry_after = deficit / rate
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(tokens), tostring(retry_after)}
"""

# Fraction of bucket capacity held back per priority. High priority reserves
# nothing (can use the whole bucket); lower priorities are shed first.
PRIORITY_RESERVE = {"high": 0.0, "normal": 0.10, "low": 0.30}


class RateLimiter:
    def __init__(self, client: redis.Redis):
        self.r = client
        self._sha: str | None = None

    async def _eval(self, key: str, rate: float, capacity: float, requested: float,
                    reserve: float = 0.0) -> tuple[bool, float, float]:
        args = [rate, capacity, time.time(), requested, 120, reserve]
        try:
            if self._sha is None:
                self._sha = await self.r.script_load(_LUA)
            res = await self.r.evalsha(self._sha, 1, key, *args)
        except redis.ResponseError:
            self._sha = await self.r.script_load(_LUA)
            res = await self.r.evalsha(self._sha, 1, key, *args)
        except redis.RedisError as e:
            logger.warning("redis unavailable, failing open: %s", e)
            return True, capacity, 0.0
        allowed, remaining, retry_after = int(res[0]), float(res[1]), float(res[2])
        return bool(allowed), remaining, retry_after

    async def check_request(self, team: TeamConfig) -> tuple[bool, int]:
        """Consume one RPM token, honoring the team's priority reserve.

        Returns (allowed, retry_after_seconds). A low-priority team is refused
        once its bucket drops into the reserved headroom, so high-priority
        traffic keeps flowing when the system is under pressure.
        """
        rpm = team.rate_limits.requests_per_min
        reserve = rpm * PRIORITY_RESERVE.get(team.priority, 0.0)
        allowed, _, retry = await self._eval(
            f"rl:rpm:{team.id}", rate=rpm / 60.0, capacity=rpm, requested=1, reserve=reserve)
        return allowed, int(retry) + 1

    async def check_tokens(self, team: TeamConfig, tokens: int) -> tuple[bool, int]:
        """Consume `tokens` from the TPM bucket. Returns (allowed, retry_after_seconds)."""
        tpm = team.rate_limits.tokens_per_min
        allowed, _, retry = await self._eval(
            f"rl:tpm:{team.id}", rate=tpm / 60.0, capacity=tpm, requested=max(1, tokens))
        return allowed, int(retry) + 1

    async def refund_request(self, team: TeamConfig) -> None:
        """Give back one RPM token (used when a later check in the same request fails)."""
        rpm = team.rate_limits.requests_per_min
        await self._eval(f"rl:rpm:{team.id}", rate=rpm / 60.0, capacity=rpm, requested=-1)

    async def status(self, team: TeamConfig) -> dict:
        """Non-consuming peek used by the Admin API."""
        rpm = team.rate_limits.requests_per_min
        tpm = team.rate_limits.tokens_per_min
        _, rpm_left, _ = await self._eval(f"rl:rpm:{team.id}", rpm / 60.0, rpm, 0)
        _, tpm_left, _ = await self._eval(f"rl:tpm:{team.id}", tpm / 60.0, tpm, 0)
        return {
            "requests_per_min": rpm, "requests_remaining": int(rpm_left),
            "tokens_per_min": tpm, "tokens_remaining": int(tpm_left),
        }
