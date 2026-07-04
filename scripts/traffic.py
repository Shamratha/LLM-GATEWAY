"""Continuous traffic generator — keeps the Grafana dashboards moving.

Streams steady, varied load (multiple teams + models) and periodically injects
a provider outage so you can watch fallback + circuit-breaker panels react.
Runs until you stop it (Ctrl-C).

    python scripts/traffic.py                 # ~8 requests/sec, forever
    python scripts/traffic.py --rps 20        # heavier
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import random

import httpx

GATEWAY = "http://localhost:8080"
ADMIN_KEY = "changeme-admin-key"

# (api_key, model) pairs — mixes teams and allowed models.
TRAFFIC = [
    ("sk-alpha-pro-0001", "mock-fast"),
    ("sk-alpha-pro-0001", "mock-backup"),
    ("sk-bravo-start-0002", "mock-fast"),     # tight 10 rpm → generates 429s
    ("sk-batch-lowpri-0003", "mock-fast"),
]

PROMPTS = ["hello", "explain caching", "what is a token bucket?",
           "summarize this", "translate to french", "write a haiku"]


async def one(client, key, model):
    try:
        await client.post(f"{GATEWAY}/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model,
                                "messages": [{"role": "user", "content": random.choice(PROMPTS)}]})
    except Exception:
        pass


async def chaos_cycle(client):
    """Every ~30s, take mock-fast down for ~10s to trigger fallback + circuit."""
    while True:
        await asyncio.sleep(30)
        await client.post(f"{GATEWAY}/admin/chaos", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                          json={"model": "mock-fast", "fail_rate": 1.0})
        print(">> chaos ON  (mock-fast failing) — watch fallback + circuit panels")
        await asyncio.sleep(10)
        await client.post(f"{GATEWAY}/admin/chaos/reset",
                          headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        print(">> chaos OFF (recovered)")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rps", type=int, default=8, help="approx requests per second")
    args = ap.parse_args()
    interval = 1.0 / args.rps
    rr = itertools.cycle(TRAFFIC)

    print(f"generating ~{args.rps} req/s to {GATEWAY} — Ctrl-C to stop")
    print("open Grafana: http://localhost:3001 (Last 15m, refresh 5s)")
    async with httpx.AsyncClient(timeout=15) as client:
        asyncio.create_task(chaos_cycle(client))
        n = 0
        while True:
            key, model = next(rr)
            asyncio.create_task(one(client, key, model))
            n += 1
            if n % 50 == 0:
                print(f"sent {n} requests")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
