"""Concurrent load test. Measures gateway overhead and rate-limit behavior.

Usage:
    python scripts/loadtest.py --requests 5000 --concurrency 100

Uses the mock provider so results reflect gateway overhead, not provider
latency. The gateway reports its own added latency per request in
gateway.overhead_ms; we aggregate that here.
"""
from __future__ import annotations

import argparse
import asyncio
import time

import httpx

GATEWAY = "http://localhost:8080"
KEYS = ["sk-alpha-pro-0001", "sk-batch-lowpri-0003"]   # higher-limit teams


async def worker(client, key, results):
    try:
        r = await client.post(f"{GATEWAY}/v1/chat/completions", headers={
            "Authorization": f"Bearer {key}"}, json={
            "model": "mock-fast", "messages": [{"role": "user", "content": "ping"}]})
        if r.status_code == 200:
            results["ok"] += 1
            results["overhead"].append(r.json()["gateway"]["overhead_ms"])
        elif r.status_code == 429:
            results["rate_limited"] += 1
        else:
            results["errors"] += 1
    except Exception:
        results["errors"] += 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=5000)
    ap.add_argument("--concurrency", type=int, default=100)
    args = ap.parse_args()

    results = {"ok": 0, "rate_limited": 0, "errors": 0, "overhead": []}
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(
            max_connections=args.concurrency, max_keepalive_connections=args.concurrency)) as client:
        async def bounded(i):
            async with sem:
                await worker(client, KEYS[i % len(KEYS)], results)

        start = time.perf_counter()
        await asyncio.gather(*(bounded(i) for i in range(args.requests)))
        elapsed = time.perf_counter() - start

    ov = sorted(results["overhead"])
    def pct(p): return ov[int(len(ov) * p)] if ov else 0.0
    print(f"\nrequests={args.requests} concurrency={args.concurrency} elapsed={elapsed:.2f}s")
    print(f"throughput: {args.requests / elapsed:,.0f} req/s")
    print(f"ok={results['ok']} rate_limited={results['rate_limited']} errors={results['errors']}")
    if ov:
        print(f"gateway overhead ms — p50={pct(0.5):.2f} p95={pct(0.95):.2f} p99={pct(0.99):.2f}")


if __name__ == "__main__":
    asyncio.run(main())
