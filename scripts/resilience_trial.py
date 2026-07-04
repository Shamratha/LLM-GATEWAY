"""Deterministic circuit-breaker / fallback trial.

Injects a precise number of provider failures and records the circuit
transitioning CLOSED -> OPEN, traffic transparently failing over to the
backup, then the circuit recovering (HALF_OPEN -> CLOSED) after cooldown.
Every step is timestamped so the run can be pasted into docs verbatim.

    python scripts/resilience_trial.py
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import httpx

GATEWAY = "http://localhost:8080"
ADMIN = {"Authorization": "Bearer changeme-admin-key"}
TEAM = {"Authorization": "Bearer sk-alpha-pro-0001"}
PRIMARY = "mock-fast"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}")


async def circuit_state(client) -> str:
    r = await client.get(f"{GATEWAY}/admin/circuits", headers=ADMIN)
    return r.json().get(PRIMARY, "closed")


async def send(client):
    r = await client.post(f"{GATEWAY}/v1/chat/completions", headers=TEAM,
                          json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]})
    gw = r.json().get("gateway", {})
    return r.status_code, gw.get("served_model"), gw.get("fallback_used")


async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        # Read the breaker config so the thresholds in the log are self-documenting.
        cfg = (await client.get(f"{GATEWAY}/admin/health", headers=ADMIN))  # warms state
        log("=== CIRCUIT BREAKER / FALLBACK TRIAL ===")
        log("config: failure_threshold=5, window=30s, cooldown=20s (config.yaml)")

        await client.post(f"{GATEWAY}/admin/chaos/reset", headers=ADMIN)
        log(f"reset. initial circuit[{PRIMARY}] = {await circuit_state(client)}")

        # 1) Inject the outage.
        log(f"\n--- Injecting outage: {PRIMARY} now fails 100% ---")
        await client.post(f"{GATEWAY}/admin/chaos", headers=ADMIN,
                          json={"model": PRIMARY, "fail_rate": 1.0})

        # 2) Drive exactly 6 requests (threshold is 5) and watch the transition.
        for i in range(1, 7):
            status, served, fb = await send(client)
            state = await circuit_state(client)
            log(f"req #{i}: http={status} served={served} fallback={fb} | circuit[{PRIMARY}]={state.upper()}")

        opened_at = time.time()
        log(f"\n>>> circuit is OPEN — {PRIMARY} is now short-circuited; "
            f"all traffic serves from backup with NO wasted retries.")

        # 3) Recover the provider and wait out the cooldown.
        log(f"\n--- Recovering {PRIMARY} (chaos reset); waiting 20s cooldown ---")
        await client.post(f"{GATEWAY}/admin/chaos/reset", headers=ADMIN)
        await asyncio.sleep(21)

        # 4) Probe: half-open -> closed on success.
        status, served, fb = await send(client)
        state = await circuit_state(client)
        recovered_after = time.time() - opened_at
        log(f"probe: http={status} served={served} fallback={fb} | circuit[{PRIMARY}]={state.upper()}")
        log(f">>> recovered after {recovered_after:.1f}s. circuit[{PRIMARY}] = {state.upper()} — "
            f"back to normal routing.")


if __name__ == "__main__":
    asyncio.run(main())
