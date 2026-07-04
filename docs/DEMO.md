# Demo walkthrough (≈4-minute recording script)

This is a ready-to-record script for a portfolio demo video. Each scene has the
command to run, the real output to expect, and a one-line narration. Total
runtime ~4 minutes. All of it runs on the mock provider — **no API keys, no
cost.**

## Setup (before recording)

```bash
cd llm-gateway
docker compose up -d --build          # gateway :8080, Redis, Prometheus :9090, Grafana :3001
python scripts/traffic.py --rps 10    # background: keeps the Grafana dashboards live
```
Open **Grafana → http://localhost:3001** (login `admin`/`admin`), set the time
range to **Last 15 minutes** and auto-refresh to **5s**. Keep it on a second
monitor — you'll cut to it between scenes.

---

## Scene 1 — One request, end to end (~30s)

> "Every team's LLM call goes through one OpenAI-compatible endpoint. Watch the
> gateway's own metadata come back with the answer."

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-alpha-pro-0001" -H "Content-Type: application/json" \
  -d '{"model":"mock-fast","messages":[{"role":"user","content":"hello gateway"}]}'
```
```json
{ ..., "model":"mock-fast",
  "choices":[{"message":{"role":"assistant","content":"[mock-fast] You said: hello gateway"}}],
  "usage":{"prompt_tokens":4,"completion_tokens":8,"total_tokens":12},
  "gateway":{"served_model":"mock-fast","fallback_used":false,"cost_usd":1.6e-05,"overhead_ms":7.89} }
```
Point at `gateway.cost_usd` and `overhead_ms` — per-request cost and the latency
the gateway adds.

## Scene 2 — Rate limiting under a burst (~40s)

> "The Starter team is capped at 10 requests/minute. I'll fire 20 at once."

```bash
python scripts/demo.py     # scene 2 section, or the snippet below
```
Real output:
```
=== 2. Rate limiting (Starter team, 10 req/min) ===
200 OK: 10   429 rate-limited: 10  (Retry-After enforced)
```
> "Exactly 10 served, 10 rejected with HTTP 429 and a Retry-After header — the
> distributed Redis token bucket, enforced atomically."

## Scene 3 — Automatic fallback during an outage (~50s)

> "I'll kill the primary provider mid-flight and show the caller never notices."

```bash
# inject the outage
curl -s -X POST http://localhost:8080/admin/chaos -H "Authorization: Bearer changeme-admin-key" \
  -H "Content-Type: application/json" -d '{"model":"mock-fast","down":true}'
# same request as scene 1
curl -s http://localhost:8080/v1/chat/completions -H "Authorization: Bearer sk-alpha-pro-0001" \
  -H "Content-Type: application/json" -d '{"model":"mock-fast","messages":[{"role":"user","content":"hi"}]}'
```
```json
{ "model":"mock-backup", ...,
  "gateway":{"requested_model":"mock-fast","served_model":"mock-backup","fallback_used":true,
             "attempted":[{"model":"mock-fast","error":"mock model 'mock-fast' is down"},
                          {"model":"mock-backup","ok":true}]} }
```
> "Requested `mock-fast`, served `mock-backup`, still a 200. The `attempted`
> trail shows the retry, then the transparent failover."

## Scene 4 — Circuit breaker opening and recovering (~60s)

> "Sustained failures should stop the gateway from even trying a dead provider —
> and then recover on its own."

```bash
python scripts/resilience_trial.py
```
Real timeline:
```
[..:34.119] reset. initial circuit[mock-fast] = closed
--- Injecting outage: mock-fast now fails 100% ---
[..:34.931] req #1: served=mock-backup fallback=True | circuit[mock-fast]=CLOSED
[..:35.613] req #2: served=mock-backup fallback=True | circuit[mock-fast]=CLOSED
[..:36.220] req #3: served=mock-backup fallback=True | circuit[mock-fast]=CLOSED
[..:36.900] req #4: served=mock-backup fallback=True | circuit[mock-fast]=CLOSED
[..:37.599] req #5: served=mock-backup fallback=True | circuit[mock-fast]=OPEN    ← 5th failure trips it
[..:37.702] req #6: served=mock-backup fallback=True | circuit[mock-fast]=OPEN    ← short-circuited
--- Recovering mock-fast (chaos reset); waiting 20s cooldown ---
[..:58.777] probe:  served=mock-fast  fallback=False | circuit[mock-fast]=CLOSED  ← recovered
>>> recovered after 21.1s. back to normal routing.
```
> "The circuit opens on exactly the 5th failure, short-circuits further calls, and
> a single half-open probe closes it after the cooldown — no restart."

## Scene 5 — The dashboards (~30s)

Cut to Grafana. Walk the three dashboards:
- **Operations** — provider health, circuit-breaker state, fallback spikes.
- **Business** — spend per team, request rates, rejections.
- **Performance** — latency percentiles, token throughput, gateway overhead.

Then **Alerting → Alert rules**: the 4 rules (error rate, latency SLA,
circuit-open, provider-down) showing live Normal/Firing status.

> "Everything you just saw is recorded per team, per provider — one pane of glass
> across every LLM call."

---

## Closing line

> "An LLM API gateway with per-team rate limits and budgets, automatic
> multi-provider failover, circuit breaking, and full observability — ~5ms of
> gateway overhead per request, behind a single OpenAI-compatible endpoint."

## Reset after recording
```bash
curl -s -X POST http://localhost:8080/admin/chaos/reset -H "Authorization: Bearer changeme-admin-key"
# stop the background traffic generator (Ctrl-C in its terminal)
```
