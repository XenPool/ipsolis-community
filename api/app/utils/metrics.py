"""Prometheus metrics for ipSolis.

Two flavours of metric:

* Always-on counters / histograms updated by the request middleware
  (HTTP request volume + latency, route-level).
* Business gauges refreshed on demand whenever ``/metrics`` is scraped
  (orders by status, pending approvals, pool free/busy by asset type).

Scrapes hit the database with a handful of indexed ``SELECT count(*) GROUP BY``
queries — cheap enough at the typical 15-60s Prometheus scrape interval that
no caching layer is needed. Disable via ``metrics.enabled = false`` if a
zero-load endpoint is preferred.
"""
from __future__ import annotations

import logging
from typing import Iterable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import OrderApproval
from app.models.asset import AssetPool, AssetType
from app.models.order import Order

logger = logging.getLogger(__name__)

# Module-private registry — keeps ipSolis metrics isolated from the global
# default registry that prometheus_client uses for its own internals.
REGISTRY = CollectorRegistry()

# Bucket choice tuned for an internal admin/portal app: most requests should
# land sub-100ms, with a long tail for runbook-dispatching endpoints.
_LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1,
    0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

# ── HTTP middleware metrics ───────────────────────────────────────────────────
http_requests_total = Counter(
    "ipsolis_http_requests_total",
    "Total HTTP requests, labelled by method, route template and status class.",
    ["method", "route", "status_class"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "ipsolis_http_request_duration_seconds",
    "HTTP request duration in seconds, by method and route template.",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

# ── Business gauges (refreshed on /metrics scrape) ────────────────────────────
orders_in_status = Gauge(
    "ipsolis_orders_in_status",
    "Number of orders currently in each lifecycle status.",
    ["status"],
    registry=REGISTRY,
)

approvals_pending = Gauge(
    "ipsolis_approvals_pending",
    "Number of approval rows still in the 'pending' state.",
    registry=REGISTRY,
)

pool_assets = Gauge(
    "ipsolis_pool_assets",
    "Asset pool size, by asset type name and status.",
    ["asset_type", "status"],
    registry=REGISTRY,
)

celery_queue_depth = Gauge(
    "ipsolis_celery_queue_depth",
    "Number of pending tasks per Celery queue (Redis LLEN).",
    ["queue"],
    registry=REGISTRY,
)

# Queues actually used by ipSolis. Worker.task_routes maps tasks here:
# default        — license check, maintenance, SIEM streamer
# provision      — order dispatch, scheduled-runbook trigger, PS module install
# reclaim        — DELETE actions and expiry-driven reclaim
# notifications  — approval emails, approval reminders + escalation
_KNOWN_QUEUES: tuple[str, ...] = ("default", "provision", "reclaim", "notifications")


def record_request(method: str, route: str, status_code: int, duration_seconds: float) -> None:
    """Bump the HTTP counter + histogram for a completed request."""
    status_class = f"{status_code // 100}xx"
    http_requests_total.labels(method=method, route=route, status_class=status_class).inc()
    http_request_duration_seconds.labels(method=method, route=route).observe(duration_seconds)


async def _refresh_business_gauges(db: AsyncSession) -> None:
    """Re-populate the gauges from a fresh DB snapshot.

    Called once per scrape. Reset-and-set semantics so labels for
    statuses with zero rows disappear instead of going stale.
    """
    # Orders by status
    orders_in_status.clear()
    rows = await db.execute(
        select(Order.status, func.count()).group_by(Order.status)
    )
    for status_value, count in rows.all():
        label = status_value.value if hasattr(status_value, "value") else str(status_value)
        orders_in_status.labels(status=label).set(count)

    # Approvals pending
    pending = await db.execute(
        select(func.count())
        .select_from(OrderApproval)
        .where(OrderApproval.status == "pending")
    )
    approvals_pending.set(pending.scalar_one())

    # Pool by (asset_type_name, asset_status)
    pool_assets.clear()
    pool_rows = await db.execute(
        select(AssetType.name, AssetPool.status, func.count())
        .join(AssetType, AssetType.id == AssetPool.asset_type_id)
        .group_by(AssetType.name, AssetPool.status)
    )
    for type_name, status_value, count in pool_rows.all():
        label = status_value.value if hasattr(status_value, "value") else str(status_value)
        pool_assets.labels(asset_type=type_name, status=label).set(count)


async def _refresh_celery_gauges() -> None:
    """Set ``ipsolis_celery_queue_depth{queue}`` from Redis ``LLEN`` per queue.

    Redis is the broker; Celery stores pending messages as plain Redis lists
    keyed by the queue name. ``LLEN`` returns the depth in O(1). When the
    broker URL is missing or non-Redis, the gauges stay at zero (cleared).
    """
    import os
    broker = os.environ.get("CELERY_BROKER_URL", "")
    if not broker.startswith(("redis://", "rediss://")):
        celery_queue_depth.clear()
        return

    try:
        import redis.asyncio as aioredis
    except ImportError as exc:
        logger.warning("metrics: redis client unavailable: %s", exc)
        return

    client = aioredis.from_url(broker)
    try:
        celery_queue_depth.clear()
        for queue in _KNOWN_QUEUES:
            try:
                depth = await client.llen(queue)
            except Exception as exc:  # noqa: BLE001 — per-queue failures shouldn't tank the rest
                logger.warning("metrics: LLEN %s failed: %s", queue, exc)
                continue
            celery_queue_depth.labels(queue=queue).set(int(depth or 0))
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


async def render(db: AsyncSession) -> tuple[bytes, str]:
    """Return ``(payload, content_type)`` for the /metrics response."""
    try:
        await _refresh_business_gauges(db)
    except Exception as exc:  # noqa: BLE001 — never fail a scrape on a transient DB error
        logger.warning("metrics: gauge refresh failed: %s", exc)
    try:
        await _refresh_celery_gauges()
    except Exception as exc:  # noqa: BLE001
        logger.warning("metrics: celery queue refresh failed: %s", exc)
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# ── Helpers for the middleware ────────────────────────────────────────────────

def safe_route_template(scope_route_path: str | None, fallback: str) -> str:
    """Return a low-cardinality route label.

    FastAPI populates ``scope['route'].path`` with the matched route's
    template (e.g. ``/orders/{order_id}``). For unmatched paths
    (404 before routing, /static/*, /metrics itself) we collapse to a
    single label so request volume doesn't explode the time series.
    """
    if not scope_route_path:
        return fallback
    return scope_route_path


# Paths whose request-count would be high-volume and noisy if labelled
# individually (per-file static lookups, locale JSON files).
_BUCKETED_PREFIXES: Iterable[str] = ("/static/", "/locales/")


def collapse_high_volume_paths(path: str) -> str | None:
    """Collapse known high-volume static-asset paths to a single label."""
    for prefix in _BUCKETED_PREFIXES:
        if path.startswith(prefix):
            return prefix.rstrip("/") + "/*"
    return None
