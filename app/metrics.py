"""Prometheus metrics. Exposed at /metrics for Prometheus to scrape."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "gateway_requests_total", "Requests handled",
    ["team", "model", "provider", "status"],
)
ERRORS = Counter(
    "gateway_errors_total", "Errors by type",
    ["team", "provider", "error_type"],
)
LATENCY = Histogram(
    "gateway_request_latency_seconds", "End-to-end gateway latency",
    ["provider"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
OVERHEAD = Histogram(
    "gateway_overhead_seconds", "Gateway-added latency (excludes provider call)",
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1),
)
TOKENS = Counter(
    "gateway_tokens_total", "Tokens processed",
    ["team", "model", "direction"],   # direction = input | output
)
COST = Counter(
    "gateway_cost_usd_total", "Estimated spend in USD",
    ["team", "model"],
)
FALLBACKS = Counter(
    "gateway_fallback_total", "Fallback activations",
    ["from_model", "to_model"],
)
RATELIMIT_REJECTS = Counter(
    "gateway_ratelimit_rejections_total", "Requests rejected by rate limiter",
    ["team", "reason"],   # reason = rpm | tpm
)
BUDGET_REJECTS = Counter(
    "gateway_budget_rejections_total", "Requests rejected by budget cap",
    ["team"],
)
CIRCUIT_STATE = Gauge(
    "gateway_circuit_state", "Circuit breaker state (0=closed,1=open,2=half_open)",
    ["provider_model"],
)
PROVIDER_HEALTH = Gauge(
    "gateway_provider_health", "Provider health (1=healthy,0.5=degraded,0=down)",
    ["provider_model"],
)
