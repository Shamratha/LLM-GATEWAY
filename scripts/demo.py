"""Interactive demo: drives the gateway through its headline scenarios.

Run the stack first (docker compose up -d, or uvicorn locally), then:
    python scripts/demo.py

Scenarios:
  1. A normal chat request round-trips through the gateway.
  2. Rate limiting: hammer the Starter team until it gets 429s.
  3. Fallback: mark the primary mock model "down", watch traffic fall back.
  4. Circuit breaker: sustained failures open the circuit.
"""
from __future__ import annotations

import asyncio

import httpx

GATEWAY = "http://localhost:8080"
ADMIN_KEY = "changeme-admin-key"

ALPHA = "sk-alpha-pro-0001"
BRAVO = "sk-bravo-start-0002"   # Starter: 10 req/min


def hdr(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def chat(client: httpx.AsyncClient, key: str, model="mock-fast", content="Hello!"):
    return await client.post(f"{GATEWAY}/v1/chat/completions", headers=hdr(key), json={
        "model": model, "messages": [{"role": "user", "content": content}],
    })


async def scenario_normal(client):
    print("\n=== 1. Normal request ===")
    r = await chat(client, ALPHA, content="Summarize the CAP theorem in one line.")
    data = r.json()
    print("status:", r.status_code)
    print("served_model:", data["gateway"]["served_model"])
    print("answer:", data["choices"][0]["message"]["content"])
    print("cost_usd:", data["gateway"]["cost_usd"], "overhead_ms:", data["gateway"]["overhead_ms"])


async def scenario_rate_limit(client):
    print("\n=== 2. Rate limiting (Starter team, 10 req/min) ===")
    ok = rejected = 0
    for _ in range(20):
        r = await chat(client, BRAVO)
        if r.status_code == 200:
            ok += 1
        elif r.status_code == 429:
            rejected += 1
    print(f"200 OK: {ok}   429 rate-limited: {rejected}  (Retry-After enforced)")


async def scenario_fallback(client):
    print("\n=== 3. Fallback routing ===")
    await client.post(f"{GATEWAY}/admin/chaos", headers=hdr(ADMIN_KEY),
                      json={"model": "mock-fast", "down": True})
    print("injected: mock-fast is DOWN")
    r = await chat(client, ALPHA)
    data = r.json()
    print("requested:", data["gateway"]["requested_model"],
          "-> served:", data["gateway"]["served_model"],
          "| fallback_used:", data["gateway"]["fallback_used"])
    await client.post(f"{GATEWAY}/admin/chaos/reset", headers=hdr(ADMIN_KEY))
    print("chaos reset")


async def scenario_circuit(client):
    print("\n=== 4. Circuit breaker ===")
    await client.post(f"{GATEWAY}/admin/chaos", headers=hdr(ADMIN_KEY),
                      json={"model": "mock-fast", "fail_rate": 1.0})
    print("injected: mock-fast fails 100%")
    for _ in range(8):
        await chat(client, ALPHA)
    circuits = (await client.get(f"{GATEWAY}/admin/circuits", headers=hdr(ADMIN_KEY))).json()
    print("circuit states:", circuits)
    await client.post(f"{GATEWAY}/admin/chaos/reset", headers=hdr(ADMIN_KEY))
    print("chaos reset (circuit will half-open then close after cooldown)")


async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        await scenario_normal(client)
        await scenario_rate_limit(client)
        await scenario_fallback(client)
        await scenario_circuit(client)
        print("\nDone. Open Grafana at http://localhost:3000 to see the metrics.")


if __name__ == "__main__":
    asyncio.run(main())
