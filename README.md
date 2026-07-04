# LLM Gateway

A production-style API gateway that sits in front of every LLM call an
organization makes. It enforces **per-team rate limits and budgets**,
**automatically falls back** to alternate providers during outages, and gives
**unified observability** over all LLM traffic — behind a single
**OpenAI-compatible** endpoint.

> Point any OpenAI SDK at `http://localhost:8080/v1` and it just works — the
> gateway handles routing, resilience, limits, and metrics transparently.

**Live demo:** not hosted at a public URL — this is a four-service Docker Compose stack (gateway + Redis + Prometheus + Grafana) meant to run locally, so evaluation is via `docker compose up` (a hosted demo would need a paid multi-container host); the measured results and resilience trial below are from real local runs.

```
Client (OpenAI SDK) ──► POST /v1/chat/completions
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│  GATEWAY (FastAPI, async)                                  │
│  auth → enrich → rate-limit (RPM) → rate-limit (TPM) →     │
│  budget check → route ─┐                                   │
│                        │ circuit breaker per model         │
│                        │ retry + exponential backoff       │
│                        └► fallback chain (by tier)          │
│  every step → Prometheus metrics + OTel spans             │
└──────────────────────────────────────────────────────────┘
   │            │             │              │
 Redis      Prometheus     Providers      Grafana
(buckets,   (scrapes       (Mock / OpenAI  (dashboards)
 budgets)    /metrics)      Anthropic /
                            Ollama)
```

## Why this exists

Every company with more than one team using LLMs rebuilds some version of this.
It's infrastructure engineering applied to AI: distributed rate limiting,
retries, circuit breakers, budget enforcement, and observability — the plumbing
that keeps a fleet of LLM-powered apps reliable and on-budget.

## Measured results

Real numbers from `scripts/loadtest.py` — **5,000 requests at concurrency 100**,
mock provider, single uvicorn worker, Windows 11 + Docker Desktop. (Reproduce
with the commands under [Load testing](#load-testing).)

**Rate-limit accuracy under concurrency** (default demo limits, mixed traffic
across `team-alpha` @ 60 rpm / `normal` priority and `team-batch` @ 120 rpm /
`low` priority; run took 30.9 s):

| Metric | Value |
|---|---|
| Requests allowed (`200`) | **229** |
| Requests rejected (`429`) | 4,771 |
| Errors | **0** |
| Predicted allowed | 85 (alpha: (60−6 reserve) + 1/s·30.9s) + 146 (batch: (120−36 reserve) + 2/s·30.9s) ≈ **230** |

The distributed token bucket allowed **229 vs a predicted ~230** under 100-way
concurrency — **<0.5% error** between enforced and theoretical limits. The
`reserve` terms are the **priority headroom**: `normal` holds back 10% of its
bucket and `low` holds back 30%, so lower-priority teams are shed first under
pressure while capacity is kept for higher-priority traffic (see [Priority-based
shedding](#priority-based-shedding)).

**Gateway overhead** (limits raised so all 5,000 requests serve; overhead =
gateway time, excluding the upstream provider call):

| Path | Throughput | Served | Errors | Overhead p50 | p95 | p99 |
|---|---|---|---|---|---|---|
| via host port-forward | 80 req/s | 5,000/5,000 | 0 | 5.1 ms | 10.7 ms | 61 ms |
| in-network (no port-forward) | 139 req/s | 5,000/5,000 | 0 | 5.4 ms | 14.8 ms | 85 ms |

Median gateway-added latency is **~5 ms** and p95 **~11–15 ms** (dominated by the
~5 Redis round-trips per request for rate-limit + budget enforcement). The p99
tail (tens of ms, and it swaps between the two paths run-to-run) is **jitter, not
a systematic cost** — CPython GC pauses and single-event-loop scheduling under
100-way concurrency — which is why p50/p95 stay stable.

**On throughput — an honest note.** A multi-worker experiment (1 vs 4 uvicorn
workers, driven by 1 and by 4 parallel load-generators) plateaued at **~160 req/s
regardless of worker or client count**. That's not the gateway's ceiling: by
Little's law, ~240 concurrent requests at 160 req/s implies ~1.5 s of end-to-end
queueing, while the gateway reports only **~7 ms of its own work** per request —
the requests are stuck in the Docker Desktop VM's virtualized loopback across the
per-request Redis round-trips, not in gateway code. The gateway holds no
per-request state outside Redis (only the circuit breaker is per-replica, by
design), so it scales horizontally across hosts — but **honestly demonstrating
that requires distributed load generation and a production Redis, which this
single-laptop setup can't provide.** The throughput figures above are a floor set
by the dev box; the meaningful, environment-independent number is the ~5 ms
median overhead the gateway adds.

## Resilience trial (demonstrated)

Deterministic circuit-breaker + fallback run via `scripts/resilience_trial.py`,
which injects a precise outage through the `/admin/chaos` endpoint. Real,
unedited timeline — `mock-fast` forced to fail 100%, threshold = 5:

```
[01:37:34.119] reset. initial circuit[mock-fast] = closed
--- Injecting outage: mock-fast now fails 100% ---
[01:37:34.931] req #1: http=200 served=mock-backup fallback=True | circuit[mock-fast]=CLOSED
[01:37:35.613] req #2: http=200 served=mock-backup fallback=True | circuit[mock-fast]=CLOSED
[01:37:36.220] req #3: http=200 served=mock-backup fallback=True | circuit[mock-fast]=CLOSED
[01:37:36.900] req #4: http=200 served=mock-backup fallback=True | circuit[mock-fast]=CLOSED
[01:37:37.599] req #5: http=200 served=mock-backup fallback=True | circuit[mock-fast]=OPEN   ← 5th failure trips it
[01:37:37.702] req #6: http=200 served=mock-backup fallback=True | circuit[mock-fast]=OPEN   ← short-circuited, no retries
--- Recovering mock-fast (chaos reset); waiting 20s cooldown ---
[01:37:58.777] probe: http=200 served=mock-fast  fallback=False | circuit[mock-fast]=CLOSED  ← recovered
>>> recovered after 21.1s. back to normal routing.
```

The gateway's own structured logs confirm every state transition:

```
gateway.circuit  circuit mock-fast: closed -> open
gateway.circuit  circuit mock-fast: open -> half_open
gateway.circuit  circuit mock-fast: half_open -> closed
```

**What this proves:** every client request stayed `200` throughout the outage
(transparent failover to the backup), the circuit opened on exactly the 5th
failure, subsequent requests were short-circuited (no wasted retries against the
dead provider), and a single half-open probe recovered the primary after the
20 s cooldown — no restart, no manual intervention.

## Testing & coverage

**20 tests, all passing** (`pytest`): 7 end-to-end integration tests through the
real ASGI app against an in-memory Redis, 6 resilience unit tests for the circuit
breaker and router fallback, 3 rate-limiter tests (incl. priority shedding), and
4 provider contract tests that verify request translation + response
normalization against a mocked HTTP transport. Overall line coverage **79%**.

Coverage weighted toward the **safety-critical modules** — the ones that gate
spend and quota, where a bug means real money or an outage:

| Module | Coverage | Role | Risk if wrong |
|---|---|---|---|
| `rate_limiter.py` | 70% | Redis token buckets (RPM/TPM) + priority reserve | quota bypass / DoS |
| `budget.py` | 77% | per-team spend caps | runaway spend |
| `router.py` | 79% | retry + fallback orchestration | outages not absorbed |
| `circuit_breaker.py` | 81% | trip/recover per provider | hammering dead providers |
| `auth.py` | 96% | constant-time API-key auth | unauthorized access |

The uncovered lines in the safety-critical modules are chiefly the
**Redis-outage fail-open branches** and admin status/peek helpers. Provider
adapters (`openai`/`anthropic`/`ollama`, 61–67%) have their translation and
error-mapping paths covered by contract tests against a mocked transport; the
remaining uncovered lines are streaming, which needs a live upstream. The
distributed rate limiter's Lua script is exercised against a real Lua-capable
fake Redis so the atomic check-and-consume path is genuinely tested, not stubbed.

## Features

| Capability | How it works |
|---|---|
| **OpenAI-compatible API** | `POST /v1/chat/completions` (streaming + non-streaming). Existing OpenAI SDKs work by changing `base_url`. |
| **Multi-provider** | Pluggable adapters for **Mock, OpenAI, Anthropic, Ollama**. A unified request is translated per provider and normalized back. |
| **Distributed rate limiting** | Redis token buckets (RPM + TPM) enforced atomically via a Lua script — correct across many replicas. Returns `429` + `Retry-After`. |
| **Priority-based shedding** | Each team has a priority (`high`/`normal`/`low`). Under pressure, lower-priority traffic is refused first while capacity is reserved for higher-priority traffic. |
| **Budget caps** | Per-team monthly/daily USD budgets. Cost computed from token usage × price. Warns at 80%, blocks at 100% (`402`). |
| **Automatic fallback** | Per-tier fallback chains. Retryable failures retry with backoff, then fall back to the next model. Non-retryable errors (auth, content policy) fail fast. |
| **Circuit breakers** | Per model-id: opens after N failures, cools down, half-opens with a single probe, then closes. |
| **Health monitoring** | Background probes track healthy/degraded/down + latency per model. |
| **Request enrichment** | Per-team system prompts, compliance disclaimers, and a banned-phrase content filter — policy enforced centrally. |
| **Observability** | Prometheus metrics + OpenTelemetry-ready, with three pre-built Grafana dashboards (Operations / Business / Performance) and alert rules. |
| **Hot-reloadable config** | Edit `config/config.yaml` → applied within ~1s, no restart. Admin API edits survive reloads as overrides. |
| **Admin API** | Inspect status, adjust limits/budgets live, view health & circuit state, and inject failures for demos. |

## Priority-based shedding

Not all traffic is equal: a real-time user request matters more than a batch
job. Each team declares a `priority` (`high` / `normal` / `low`), and the RPM
token bucket enforces a **reserve** per priority — a floor of tokens a caller
must leave behind:

| Priority | Reserve (of bucket) | Behavior under pressure |
|---|---|---|
| `high` | 0% | can drain the whole bucket |
| `normal` | 10% | shed once the bucket drops below 10% |
| `low` | 30% | shed first — refused below 30% |

So when a bucket runs low, `low`-priority requests get `429`s while capacity is
held back for `high`-priority traffic. This is a single atomic check in the same
Lua script (no extra round-trip), and it's covered by `tests/test_rate_limit.py`
(`test_priority_ordering_under_pressure`). The reserve fractions live in
`PRIORITY_RESERVE` in [`app/rate_limiter.py`](app/rate_limiter.py).

## Quickstart (Docker — full stack)

```bash
cp .env.example .env          # optional: add OPENAI_API_KEY / ANTHROPIC_API_KEY
docker compose up --build
```

This starts the **gateway** (`:8080`), **Redis**, **Prometheus** (`:9090`), and
**Grafana** (`:3001`, dashboards pre-provisioned, anonymous viewing enabled).

Send a request (works with zero API keys — uses the mock provider):

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-alpha-pro-0001" \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-fast","messages":[{"role":"user","content":"Hello!"}]}'
```

Run the guided demo (normal request → rate limiting → fallback → circuit breaker):

```bash
python scripts/demo.py
```

Then open **Grafana → LLM Gateway** at http://localhost:3001. For a scene-by-scene
walkthrough (and a ready-to-record ~4-minute demo script with expected output),
see [`docs/DEMO.md`](docs/DEMO.md).

## Quickstart (local, no Docker)

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
# Redis optional — the gateway fails open (allows) if Redis is unreachable.
uvicorn app.main:app --port 8080
pytest                                               # run the test suite
```

## Using it with the OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="sk-alpha-pro-0001")
resp = client.chat.completions.create(
    model="mock-fast",
    messages=[{"role": "user", "content": "Hi"}],
)
print(resp.choices[0].message.content)
```

The response includes a `gateway` block showing what really happened —
`served_model`, `fallback_used`, per-request `cost_usd`, and `overhead_ms`.

## Configuration

Everything is driven by [`config/config.yaml`](config/config.yaml):

- **providers** — upstream APIs and where to find their keys
- **models** — logical model → provider, tier, and per-1M-token pricing
- **fallback_chains** — ordered backups per tier
- **resilience** — retry / circuit-breaker / health-check tuning
- **teams** — API keys, allowed models, rate limits, budgets, enrichment

Three demo teams ship out of the box: `team-alpha` (Pro), `team-bravo`
(Starter, tight limits), and `team-batch` (low priority). To use real providers,
set the keys in `.env` and point the fallback chains at `gpt-4o` / `claude-sonnet`.

## Admin API

All routes require `Authorization: Bearer $ADMIN_API_KEY`.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/admin/teams` | List teams and their policy |
| `GET` | `/admin/teams/{id}/status` | Live rate-limit + budget usage |
| `POST` | `/admin/teams/{id}/limits` | Change RPM/TPM without restart |
| `POST` | `/admin/teams/{id}/budget` | Change budget without restart |
| `GET` | `/admin/health` | Provider health snapshot |
| `GET` | `/admin/circuits` | Circuit breaker states |
| `POST` | `/admin/reload` | Force a config reload |
| `POST` | `/admin/chaos` | Inject latency/failures into a mock model (demos) |

## Load testing

```bash
python scripts/loadtest.py --requests 5000 --concurrency 100
```

Reports throughput, rate-limit accuracy, and gateway overhead percentiles
(p50/p95/p99). The gateway self-reports added latency per request via
`gateway.overhead_ms`, aggregated by the load tester.

## Project layout

```
app/
  main.py            # FastAPI app + request pipeline
  models.py          # OpenAI-compatible schemas + errors
  config.py          # env settings + hot-reloadable YAML policy
  auth.py            # team / admin API-key auth
  enrichment.py      # per-team prompt/disclaimer/content-filter injection
  rate_limiter.py    # Redis token buckets (Lua, atomic)
  budget.py          # per-team spend tracking + caps
  circuit_breaker.py # per-model circuit breaker
  health.py          # background provider health probes
  router.py          # retry + fallback orchestration
  metrics.py         # Prometheus metrics
  admin.py           # admin API + runtime overrides
  providers/         # base + mock / openai / anthropic / ollama adapters
config/config.yaml   # providers, models, teams, limits, fallback chains
prometheus/          # scrape config + alert rules
grafana/             # datasource + dashboards (auto-provisioned)
scripts/             # demo.py, loadtest.py, resilience_trial.py, traffic.py
tests/               # integration, resilience, rate-limit + provider contract tests
.github/workflows/   # CI (pytest on push/PR)
```

## Tech stack

Python 3.11+ · FastAPI · httpx · Redis · Prometheus · Grafana ·
OpenTelemetry · Docker Compose.

## Design notes & trade-offs

- **Rate limiting is distributed; circuit breakers are per-replica.** Buckets and
  budgets live in Redis (shared), so limits hold across replicas. Circuit-breaker
  state is in-memory for speed — a distributed variant would move it to Redis.
- **Fail-open on Redis outage.** If Redis is unreachable, the limiter/budget
  *allow* traffic rather than hard-fail — an observability dependency shouldn't
  take down the data plane. This is a deliberate availability-over-strictness call.
- **Streaming falls back only before the first byte.** Once a provider starts
  streaming we commit to it; mid-stream failover would corrupt the response.
- **Token counts are estimated** for providers that don't report usage (and for
  the mock). Real OpenAI/Anthropic/Ollama usage is used when returned.
- **Constant-time API-key comparison.** Team and admin keys are checked with
  `hmac.compare_digest` and the team lookup scans all teams without
  short-circuiting, so a valid key can't be recovered byte-by-byte via response
  timing.
