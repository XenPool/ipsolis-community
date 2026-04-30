"""Liveness + readiness checks for ip·Solis.

The endpoint is intentionally cheap — three short queries with strict
timeouts so a load balancer / external monitor that hits ``/health``
every few seconds doesn't add measurable load. Each subsystem
contributes a separate flag so an alerting integration can act on
the specific failure (DB down vs Redis broker down vs Beat dead).

* ``database`` — single ``SELECT 1`` against the primary DB.
* ``redis`` — ``PING`` against the Celery broker URL.
* ``beat`` — presence of the RedBeat distributed-lock key in Redis.
  Set when a Beat replica holds the leader lock; absent when no
  Beat replica is running. The TTL refreshes every cycle, so a
  recently-killed Beat shows up as "absent" within ~30s
  (``redbeat_lock_timeout``).

Top-level ``status`` aggregates: ``ok`` when every subsystem is
healthy, ``degraded`` otherwise. Always returns 200 — load
balancers should drive routing decisions off ``status`` rather than
HTTP code so a transiently degraded api still drains gracefully.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(tags=["health"])

# RedBeat key prefix is set in ``worker/tasks/__init__.py`` as
# ``redbeat_key_prefix="ipsolis:redbeat:"``. The distributed lock the
# leader holds lives at ``<prefix>:lock`` — note the double colon, since
# the prefix already ends with one. Keep this in lockstep with the
# worker config; if you change one, change both.
_REDBEAT_LOCK_KEY = "ipsolis:redbeat::lock"


def _resolve_version() -> str:
    """Read the running version. ``/app/VERSION`` (bind-mount) wins so
    bumping the file alone is enough — no image rebuild required.
    """
    try:
        with open("/app/VERSION", "rb") as f:
            raw = f.read()
    except OSError:
        raw = b""
    for codec in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            v = raw.decode(codec).strip()
        except (UnicodeDecodeError, UnicodeError):
            continue
        if v:
            return v
    return (os.environ.get("APP_VERSION") or "0.0.0").strip() or "0.0.0"


_VERSION = _resolve_version()


async def _check_redis_and_beat() -> tuple[bool, bool]:
    """Probe Redis (Celery broker) and the RedBeat lock key.

    Returns ``(redis_ok, beat_alive)``. ``beat_alive`` requires Redis
    to be reachable AND the lock key to exist. A Redis outage shows as
    ``redis_ok=False`` AND ``beat_alive=False`` since the lock check
    couldn't run; that's the right shape for an alerting rule that
    cares about "is Beat actually dispatching" — a Redis outage and a
    dead Beat both block dispatch.
    """
    broker_url = (
        os.environ.get("CELERY_BROKER_URL")
        or os.environ.get("REDIS_URL")
        or "redis://redis:6379/0"
    )
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]
    except ImportError:
        return False, False

    redis_ok = False
    beat_alive = False
    try:
        client = aioredis.from_url(broker_url, socket_timeout=2, socket_connect_timeout=2)
        try:
            pong = await client.ping()
            redis_ok = bool(pong)
            if redis_ok:
                exists = await client.exists(_REDBEAT_LOCK_KEY)
                beat_alive = bool(exists)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
    except Exception:
        # Connection refused, DNS error, anything else — both flags stay False.
        pass
    return redis_ok, beat_alive


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)) -> dict:
    """Liveness + readiness check covering DB / Redis / Beat-leader."""
    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    redis_ok, beat_alive = await _check_redis_and_beat()

    overall_ok = db_ok and redis_ok and beat_alive
    return {
        "status": "ok" if overall_ok else "degraded",
        "database": "ok" if db_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
        # ``beat`` semantics: ``alive`` = a Beat replica holds the
        # leader lock; ``stale`` = no replica is dispatching, periodic
        # tasks are not running.
        "beat": "alive" if beat_alive else "stale",
        "version": _VERSION,
        "service": "xp-api",
    }
