"""Shared Jinja2Templates instance used by all route modules.

Centralising the instance allows Jinja2 environment globals (e.g. app_title,
app_logo) to be set once and reflected in every template render without
per-request DB queries or per-route parameter passing.
"""

import asyncio
import os
import time

from fastapi.templating import Jinja2Templates

APP_TITLE_DEFAULT = "Ipsolis"

templates = Jinja2Templates(directory="/app/app/templates")
templates.env.globals["app_title"] = APP_TITLE_DEFAULT
templates.env.globals["app_version"] = os.environ.get("APP_VERSION", "0.0.0")
templates.env.globals["app_logo"] = False          # bool: whether a logo is configured
templates.env.globals["app_logo_position"] = "left"
templates.env.globals["app_logo_size"] = "80"
templates.env.globals["app_logo_show_title"] = "true"
templates.env.globals["app_logo_title_size"] = "12"

# Module-level cache so the /portal/logo endpoint can read the raw data URL
# without hitting the DB on every request.
_logo_cache: dict[str, str] = {"value": ""}

# With uvicorn --workers N, each worker has its own in-memory globals above.
# A PUT /admin/config on one worker won't update the others, so rendered pages
# would flap back and forth. The helper below re-reads the app.* config from DB
# on a short TTL so all workers converge within a few seconds.
_APP_CONFIG_KEYS = (
    "app.title", "app.logo", "app.logo_position", "app.logo_size",
    "app.logo_show_title", "app.logo_title_size",
)
_last_config_refresh_ts: float = 0.0
_config_refresh_ttl_seconds: float = 5.0
_config_refresh_lock: asyncio.Lock | None = None


def set_app_title(title: str) -> None:
    """Update the app title Jinja2 global (call on startup and after config save)."""
    templates.env.globals["app_title"] = title or APP_TITLE_DEFAULT


def set_app_logo_config(key: str, value: str) -> None:
    """Update a logo-related Jinja2 global for a single config key.

    Accepts:
      key  — one of 'app.logo', 'app.logo_position', 'app.logo_size'
      value — the raw config value string
    """
    if key == "app.logo":
        _logo_cache["value"] = value or ""
        templates.env.globals["app_logo"] = bool(value)
    elif key == "app.logo_position":
        templates.env.globals["app_logo_position"] = value or "left"
    elif key == "app.logo_size":
        templates.env.globals["app_logo_size"] = value or "80"
    elif key == "app.logo_show_title":
        templates.env.globals["app_logo_show_title"] = value or "true"
    elif key == "app.logo_title_size":
        templates.env.globals["app_logo_title_size"] = value or "12"


def get_app_logo() -> str:
    """Return the raw logo data URL (empty string when no logo is set)."""
    return _logo_cache["value"]


async def refresh_app_config_if_stale(force: bool = False) -> None:
    """Reload app.* config from DB into Jinja2 globals when the cache is stale.

    With multi-worker uvicorn each worker keeps its own copy of the globals,
    so a config change on one worker must propagate to the others. This
    helper is cheap (one indexed SELECT every ``_config_refresh_ttl_seconds``
    per worker) and idempotent.
    """
    global _last_config_refresh_ts, _config_refresh_lock

    now = time.monotonic()
    if not force and (now - _last_config_refresh_ts) < _config_refresh_ttl_seconds:
        return

    if _config_refresh_lock is None:
        _config_refresh_lock = asyncio.Lock()

    async with _config_refresh_lock:
        now = time.monotonic()
        if not force and (now - _last_config_refresh_ts) < _config_refresh_ttl_seconds:
            return
        try:
            from sqlalchemy import select
            from app.database import AsyncSessionLocal
            from app.models.config import AppConfig

            async with AsyncSessionLocal() as db:
                rows = await db.execute(
                    select(AppConfig).where(AppConfig.key.in_(_APP_CONFIG_KEYS))
                )
                seen = set()
                for cfg in rows.scalars().all():
                    seen.add(cfg.key)
                    if cfg.key == "app.title":
                        set_app_title(cfg.value)
                    else:
                        set_app_logo_config(cfg.key, cfg.value)
                # Keys missing from the DB → reset to defaults so a removed
                # logo also reverts here (not just on the worker that wrote).
                if "app.logo" not in seen:
                    set_app_logo_config("app.logo", "")
            _last_config_refresh_ts = now
        except Exception:
            # Swallow: a transient DB hiccup must not break page renders.
            pass
