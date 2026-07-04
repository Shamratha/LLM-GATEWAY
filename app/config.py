"""Configuration: env settings + hot-reloadable YAML policy.

`Settings` holds process-level env config (ports, Redis URL, secrets).
`GatewayConfig` is the policy (providers, models, teams, limits) loaded from
YAML and swappable at runtime — the `ConfigStore` watches the file and
atomically replaces the in-memory config on change (no restart, no dropped
requests).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("gateway.config")


# ── Process settings (env) ─────────────────────────────────────────────────
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8080
    config_path: str = "config/config.yaml"
    log_level: str = "INFO"

    redis_url: str = "redis://localhost:6379/0"

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    admin_api_key: str = "changeme-admin-key"

    # Tracing is opt-in: the console exporter is noisy and serializes stdout
    # under load. Set OTEL_ENABLED=1 (or wire an OTLP endpoint) to turn it on.
    otel_enabled: bool = False


settings = Settings()


# ── Policy domain models ────────────────────────────────────────────────────
class Pricing(BaseModel):
    input: float = 0.0   # USD per 1M input tokens
    output: float = 0.0  # USD per 1M output tokens


class ProviderConfig(BaseModel):
    type: str
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None


class ModelConfig(BaseModel):
    provider: str
    provider_model: str | None = None   # defaults to the logical name
    tier: str = "frontier"
    pricing: Pricing = Field(default_factory=Pricing)


class RateLimits(BaseModel):
    requests_per_min: int = 60
    tokens_per_min: int = 100_000


class Budget(BaseModel):
    period: Literal["monthly", "daily"] = "monthly"
    limit_usd: float = 100.0
    warn_pct: int = 80


class Enrichment(BaseModel):
    """Per-team policy injected into every request before it hits a provider."""
    system_prefix: str = ""   # prepended as a system message (standard prompt/policy)
    disclaimer: str = ""      # appended as a trailing system instruction (compliance)
    banned_phrases: list[str] = Field(default_factory=list)  # simple content filter


class TeamConfig(BaseModel):
    id: str
    name: str = ""
    api_key: str
    priority: Literal["high", "normal", "low"] = "normal"
    allowed_models: list[str] = Field(default_factory=list)
    rate_limits: RateLimits = Field(default_factory=RateLimits)
    budget: Budget = Field(default_factory=Budget)
    enrichment: Enrichment = Field(default_factory=Enrichment)


class RetryConfig(BaseModel):
    max_attempts: int = 3
    base_delay_ms: int = 200
    max_delay_ms: int = 2000


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 5
    window_seconds: int = 30
    cooldown_seconds: int = 20


class HealthCheckConfig(BaseModel):
    interval_seconds: int = 30
    timeout_seconds: int = 5


class Resilience(BaseModel):
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)


class GatewayConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    fallback_chains: dict[str, list[str]] = Field(default_factory=dict)
    resilience: Resilience = Field(default_factory=Resilience)
    teams: list[TeamConfig] = Field(default_factory=list)

    # -- convenience lookups --
    def team_by_key(self, api_key: str) -> TeamConfig | None:
        for t in self.teams:
            if t.api_key == api_key:
                return t
        return None

    def team_by_id(self, team_id: str) -> TeamConfig | None:
        for t in self.teams:
            if t.id == team_id:
                return t
        return None

    def resolve_chain(self, model: str) -> list[str]:
        """Ordered models to attempt: requested first, then its tier's chain."""
        mc = self.models.get(model)
        chain: list[str] = [model] if mc else []
        if mc:
            for m in self.fallback_chains.get(mc.tier, []):
                if m != model and m in self.models and m not in chain:
                    chain.append(m)
        return chain


def load_config(path: str) -> GatewayConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return GatewayConfig(**raw)


class ConfigStore:
    """Holds the live config and hot-reloads it when the YAML file changes."""

    def __init__(self, path: str):
        self.path = path
        self._config = load_config(path)
        self._task: asyncio.Task | None = None
        self._hooks: list = []   # callables(GatewayConfig) run after each (re)load

    @property
    def config(self) -> GatewayConfig:
        return self._config

    def on_reload(self, hook) -> None:
        """Register a callback invoked with the new config after each reload."""
        self._hooks.append(hook)

    def _run_hooks(self) -> None:
        for hook in self._hooks:
            try:
                hook(self._config)
            except Exception as e:
                logger.error("reload hook failed: %s", e)

    def reload(self) -> None:
        try:
            self._config = load_config(self.path)
            logger.info("config reloaded from %s (%d teams, %d models)",
                        self.path, len(self._config.teams), len(self._config.models))
            self._run_hooks()
        except Exception as e:  # keep serving the old config on a bad edit
            logger.error("config reload failed, keeping previous: %s", e)

    async def watch(self) -> None:
        """Background task: reload on file change (debounced by watchfiles)."""
        try:
            from watchfiles import awatch
        except ImportError:
            logger.warning("watchfiles not installed; hot reload disabled")
            return
        abspath = os.path.abspath(self.path)
        async for _ in awatch(os.path.dirname(abspath) or "."):
            self.reload()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.watch())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
