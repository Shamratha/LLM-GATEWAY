"""Test fixtures: run the real app against an in-memory (fake) Redis."""
from __future__ import annotations

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    # Force the app to use fakeredis instead of a real server.
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: fake)

    from app.main import app
    with TestClient(app) as c:
        yield c


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}
