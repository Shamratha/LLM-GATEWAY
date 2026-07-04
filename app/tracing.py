"""OpenTelemetry setup.

Auto-instruments FastAPI so every request produces a span (receipt → response).
Exports to the console by default; set OTEL_EXPORTER_OTLP_ENDPOINT and swap the
exporter to ship to a collector (Jaeger/Tempo). Kept defensive so a missing
OTel dependency never blocks startup.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("gateway.tracing")


def setup_tracing(app) -> None:
    from app.config import settings
    if not settings.otel_enabled:
        logger.info("tracing disabled (set OTEL_ENABLED=1 to enable)")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.info("opentelemetry not installed; tracing disabled")
        return

    provider = TracerProvider(resource=Resource.create({"service.name": "llm-gateway"}))
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    logger.info("tracing enabled (console exporter)")


def tracer():
    from opentelemetry import trace
    return trace.get_tracer("llm-gateway")
