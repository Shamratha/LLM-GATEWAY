"""Provider adapters + a factory that builds them from config."""
from __future__ import annotations

from app.config import GatewayConfig, ProviderConfig, settings
from app.providers.anthropic import AnthropicProvider
from app.providers.base import Provider
from app.providers.mock import MockProvider
from app.providers.ollama import OllamaProvider
from app.providers.openai import OpenAIProvider


def build_provider(name: str, cfg: ProviderConfig) -> Provider:
    if cfg.type == "mock":
        return MockProvider(name)
    if cfg.type == "openai":
        return OpenAIProvider(name, cfg.base_url or "https://api.openai.com/v1",
                              settings.openai_api_key)
    if cfg.type == "anthropic":
        return AnthropicProvider(name, cfg.base_url or "https://api.anthropic.com/v1",
                                 settings.anthropic_api_key)
    if cfg.type == "ollama":
        base = settings.ollama_base_url if cfg.base_url_env else (cfg.base_url or settings.ollama_base_url)
        return OllamaProvider(name, base)
    raise ValueError(f"unknown provider type: {cfg.type}")


def build_registry(config: GatewayConfig) -> dict[str, Provider]:
    return {name: build_provider(name, cfg) for name, cfg in config.providers.items()}


__all__ = ["Provider", "build_provider", "build_registry"]
