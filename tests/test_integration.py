"""End-to-end tests through the real ASGI app (fake Redis)."""
from __future__ import annotations

from tests.conftest import auth

ALPHA = "sk-alpha-pro-0001"
BRAVO = "sk-bravo-start-0002"
ADMIN = "changeme-admin-key"


def _chat(client, key, model="mock-fast", content="hi", stream=False):
    return client.post("/v1/chat/completions", headers=auth(key),
                       json={"model": model, "messages": [{"role": "user", "content": content}],
                             "stream": stream})


def test_normal_completion(client):
    r = _chat(client, ALPHA)
    assert r.status_code == 200
    data = r.json()
    assert data["gateway"]["served_model"] == "mock-fast"
    assert "You said: hi" in data["choices"][0]["message"]["content"]
    assert data["usage"]["total_tokens"] > 0


def test_invalid_key_rejected(client):
    r = _chat(client, "sk-not-a-real-key")
    assert r.status_code == 401


def test_model_authorization(client):
    # Bravo (Starter) is only allowed mock-fast; gpt-4o must be forbidden.
    r = _chat(client, BRAVO, model="gpt-4o")
    assert r.status_code == 403


def test_rate_limit_enforced(client):
    # Bravo = 10 req/min; a burst of 20 must yield some 429s.
    statuses = [_chat(client, BRAVO).status_code for _ in range(20)]
    assert 429 in statuses
    assert statuses.count(200) <= 12  # allow a little refill slack


def test_fallback_on_primary_down(client):
    client.post("/admin/chaos", headers=auth(ADMIN), json={"model": "mock-fast", "down": True})
    try:
        r = _chat(client, ALPHA)
        assert r.status_code == 200
        gw = r.json()["gateway"]
        assert gw["fallback_used"] is True
        assert gw["served_model"] == "mock-backup"
    finally:
        client.post("/admin/chaos/reset", headers=auth(ADMIN))


def test_admin_requires_credentials(client):
    assert client.get("/admin/teams").status_code == 403
    assert client.get("/admin/teams", headers=auth(ADMIN)).status_code == 200


def test_streaming_passthrough(client):
    r = _chat(client, ALPHA, content="stream me", stream=True)
    assert r.status_code == 200
    body = r.text
    assert "data:" in body and "[DONE]" in body


def test_metrics_exposed(client):
    _chat(client, ALPHA)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "gateway_requests_total" in r.text
