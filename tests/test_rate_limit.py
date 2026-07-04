"""Rate limiter unit tests, incl. priority-based load shedding."""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.config import RateLimits, TeamConfig
from app.rate_limiter import RateLimiter


def _team(tid: str, priority: str, rpm: int = 10) -> TeamConfig:
    return TeamConfig(id=tid, api_key=tid, priority=priority,
                      rate_limits=RateLimits(requests_per_min=rpm, tokens_per_min=1_000_000))


async def _burst(rl: RateLimiter, team: TeamConfig, n: int) -> int:
    ok = 0
    for _ in range(n):
        allowed, _ = await rl.check_request(team)
        ok += int(allowed)
    return ok


@pytest.mark.asyncio
async def test_high_priority_can_drain_bucket():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rl = RateLimiter(r)
    # reserve=0 for high → all 10 tokens usable.
    assert await _burst(rl, _team("h", "high", rpm=10), 12) == 10


@pytest.mark.asyncio
async def test_low_priority_is_shed_first():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rl = RateLimiter(r)
    # reserve=0.30*10=3 for low → only 7 requests before it hits the reserve floor.
    ok = await _burst(rl, _team("l", "low", rpm=10), 12)
    assert ok == 7


@pytest.mark.asyncio
async def test_priority_ordering_under_pressure():
    """Same RPM, but low sheds before normal, which sheds before high."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rl = RateLimiter(r)
    high = await _burst(rl, _team("h", "high", rpm=10), 12)   # reserve 0   → 10
    normal = await _burst(rl, _team("n", "normal", rpm=10), 12)  # reserve 1 → 9
    low = await _burst(rl, _team("l", "low", rpm=10), 12)     # reserve 3   → 7
    assert high > normal > low
