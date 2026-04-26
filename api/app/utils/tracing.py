"""OpenTelemetry tracing setup.

Three exporter modes, picked by ``otel.*`` config keys at startup:

* **Disabled** (default) — no provider configured, tracing is a no-op.
* **Console** — span data is printed to stdout. Useful for local
  verification without a collector. Set ``otel.console_exporter=true``.
* **OTLP HTTP** — sends traces to a collector (Jaeger, Tempo, SigNoz,
  Honeycomb, …). Set ``otel.endpoint`` to the collector's
  ``/v1/traces`` URL. Optional ``otel.headers`` for auth (e.g. SaaS
  vendor API keys), one ``key=value`` per line.

The setup is idempotent: re-running ``setup_tracing()`` only configures
the provider on first call (subsequent calls are no-ops). FastAPI and
SQLAlchemy auto-instrumentation is applied separately because it
needs the app / engine instance — see ``instrument_app()``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PROVIDER_INSTALLED = False


def _truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in ("true", "1", "yes", "on", "enabled")


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse ``otel.headers`` (one ``key=value`` per line) into a dict."""
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key:
            out[key] = val
    return out


def setup_tracing(cfg: dict[str, Any]) -> bool:
    """Configure the global ``TracerProvider`` from ``cfg``.

    ``cfg`` is a plain dict pulled from ``app_config`` keys. Returns
    ``True`` when a real exporter is wired up, ``False`` when tracing
    stays disabled (so callers can log accordingly without re-checking
    the config themselves).
    """
    global _PROVIDER_INSTALLED
    if _PROVIDER_INSTALLED:
        return True

    if not _truthy(cfg.get("otel.enabled", "false")):
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
    except ImportError as exc:
        logger.warning("OpenTelemetry SDK not installed: %s", exc)
        return False

    service_name = (cfg.get("otel.service_name") or "ipsolis-api").strip()
    service_version = os.environ.get("APP_VERSION", "0.0.0")

    resource = Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
    })
    provider = TracerProvider(resource=resource)

    exporter_added = False

    # OTLP HTTP exporter (production path)
    endpoint = (cfg.get("otel.endpoint") or "").strip()
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            headers = _parse_headers(cfg.get("otel.headers", ""))
            otlp_exporter = OTLPSpanExporter(
                endpoint=endpoint,
                headers=headers or None,
            )
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            exporter_added = True
            logger.info("OpenTelemetry: OTLP exporter → %s", endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenTelemetry: OTLP exporter setup failed: %s", exc)

    # Console exporter (debug / local verification)
    if _truthy(cfg.get("otel.console_exporter", "false")):
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        exporter_added = True
        logger.info("OpenTelemetry: console exporter enabled (spans → stdout)")

    if not exporter_added:
        logger.info(
            "OpenTelemetry: enabled=true but no exporter configured "
            "(set otel.endpoint or otel.console_exporter)"
        )
        return False

    trace.set_tracer_provider(provider)
    _PROVIDER_INSTALLED = True
    return True


def instrument_app(app, engine) -> None:
    """Wire FastAPI + SQLAlchemy auto-instrumentation onto the running app.

    No-op when tracing was never set up (the instrumentors will use
    the global no-op tracer provider so nothing is emitted).
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        logger.info("OpenTelemetry: FastAPI auto-instrumentation enabled")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenTelemetry: FastAPI instrumentation failed: %s", exc)

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        # ``engine`` is the async SQLAlchemy engine; instrument the sync
        # underlying engine so DB spans show up.
        sync_engine = getattr(engine, "sync_engine", engine)
        SQLAlchemyInstrumentor().instrument(engine=sync_engine)
        logger.info("OpenTelemetry: SQLAlchemy auto-instrumentation enabled")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenTelemetry: SQLAlchemy instrumentation failed: %s", exc)
