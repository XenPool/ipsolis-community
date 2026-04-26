"""OpenTelemetry tracing setup for the Celery worker.

Mirrors ``api/app/utils/tracing.py`` so a span context propagated via
Celery message headers parses identically on both sides. Setup runs at
module import time (before workers fork), reading ``otel.*`` keys from
``app_config`` via a synchronous psycopg2 connection — async sessions
aren't available this early in the Celery lifecycle.

When tracing is configured on **both** api and worker, an HTTP request
that dispatches a runbook task produces a single distributed trace
spanning both processes via the auto-instrumented Celery client/server
spans.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False


def _truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in ("true", "1", "yes", "on", "enabled")


def _parse_headers(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out


def _load_otel_config_sync() -> dict[str, str]:
    """Read ``otel.*`` rows from ``app_config`` via a one-shot psycopg2 call.

    Setup time is before the Celery workers fork, so we don't have the
    project's async SQLAlchemy session available. Failure here is
    treated as "tracing disabled" and never raises.
    """
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        return {}
    # Strip SQLAlchemy driver prefixes — psycopg2 itself only knows the bare
    # ``postgresql://`` scheme. The worker container ships with the sync
    # ``+psycopg2`` form; the api with the async ``+asyncpg`` form.
    sync_url = (
        database_url
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )
    try:
        import psycopg2  # type: ignore[import-not-found]
        conn = psycopg2.connect(sync_url)
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM app_config WHERE key LIKE 'otel.%'")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row[0]: (row[1] or "") for row in rows}
    except Exception as exc:  # noqa: BLE001 — never block worker startup on a transient DB hiccup
        logger.warning("[tracing] Could not read otel.* config from DB: %s", exc)
        return {}


def setup_worker_tracing() -> bool:
    """Configure TracerProvider + auto-instrument Celery & SQLAlchemy.

    Returns ``True`` when tracing is wired up, ``False`` otherwise.
    Idempotent — safe to call from multiple worker processes after fork.
    """
    global _INSTALLED
    if _INSTALLED:
        return True

    cfg = _load_otel_config_sync()
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

    # Pin the worker service name so it shows up distinct from the api in
    # trace UIs (api spans → ``ipsolis-api``, worker spans → ``ipsolis-worker``).
    base_name = (cfg.get("otel.service_name") or "ipsolis-api").strip()
    service_name = base_name.replace("ipsolis-api", "ipsolis-worker")
    if service_name == base_name:  # admin chose a custom name; tag the worker
        service_name = f"{base_name}-worker"
    service_version = os.environ.get("APP_VERSION", "0.0.0")

    resource = Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
    })
    provider = TracerProvider(resource=resource)

    exporter_added = False

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
            logger.info("OpenTelemetry (worker): OTLP exporter → %s", endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenTelemetry (worker): OTLP exporter setup failed: %s", exc)

    if _truthy(cfg.get("otel.console_exporter", "false")):
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        exporter_added = True
        logger.info("OpenTelemetry (worker): console exporter enabled")

    if not exporter_added:
        return False

    trace.set_tracer_provider(provider)

    # Auto-instrumentation. ``CeleryInstrumentor`` hooks into task signals
    # so dispatched tasks (``send_task``) and worker-side execution
    # (``task_prerun`` / ``task_postrun``) produce paired spans linked
    # via propagated context. ``SQLAlchemyInstrumentor`` adds DB query
    # spans inside task execution.
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        CeleryInstrumentor().instrument()
        logger.info("OpenTelemetry (worker): Celery auto-instrumentation enabled")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenTelemetry (worker): Celery instrumentation failed: %s", exc)

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        SQLAlchemyInstrumentor().instrument()
        logger.info("OpenTelemetry (worker): SQLAlchemy auto-instrumentation enabled")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenTelemetry (worker): SQLAlchemy instrumentation failed: %s", exc)

    _INSTALLED = True
    return True
