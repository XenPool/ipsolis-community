"""Shared Jinja2Templates instance used by all route modules.

Centralising the instance allows Jinja2 environment globals (e.g. app_title,
app_logo) to be set once and reflected in every template render without
per-request DB queries or per-route parameter passing.
"""

import asyncio
import os
import time

from fastapi.templating import Jinja2Templates

APP_TITLE_DEFAULT = "ip·Solis"

templates = Jinja2Templates(directory="/app/app/templates")

# Sanitized markdown filter — used by `| markdown` in templates.
# Imported here so registration happens once at module load.
from app.utils.markdown_render import render_markdown as _render_markdown  # noqa: E402
templates.env.filters["markdown"] = _render_markdown

# Role helpers — let any template ask "is the signed-in admin at least
# role X?" without re-implementing the rank table inline. Mirrors
# ``app.utils.rbac.role_at_least`` but operates against the request session
# (the UI-facing identity). A missing/unknown role returns the most-privileged
# default so unauthenticated render paths (e.g. error pages) don't silently
# elevate — but the UI router guarantees a session is present anyway.
from app.utils.rbac import role_at_least as _role_at_least  # noqa: E402


def _current_admin_role(request) -> str:
    if request is None:
        return "superadmin"
    try:
        return request.session.get("admin_role") or "superadmin"
    except Exception:
        return "superadmin"


def _admin_role_at_least(request, required: str) -> bool:
    return _role_at_least(_current_admin_role(request), required)


templates.env.globals["current_admin_role"] = _current_admin_role
templates.env.globals["admin_role_at_least"] = _admin_role_at_least

def _resolve_app_version() -> str:
    """Same precedence as ``app.routes.health._resolve_version``.

    Read ``/app/VERSION`` first (bind-mounted from the repo root so
    bumping the file alone is enough), fall back to the build-arg
    ``APP_VERSION`` env var, then ``"0.0.0"``. Kept in sync with the
    health route's resolver — both are read at module load and so
    the same lookup order has to apply or the sidebar footer will
    drift from the /health endpoint.

    Tolerant of the encoding the file lands in: Windows PowerShell
    5.1's ``echo "0.5.0" > VERSION`` writes UTF-16 LE with BOM, which
    a naive utf-8 decoder rejects. We try utf-8-sig → utf-16 → utf-8
    and accept the first that yields a non-empty stripped string;
    a completely unparseable file falls through to ``APP_VERSION``
    env rather than crashing module import (which would 500 every
    template render — i.e. take portal + ui offline).
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


templates.env.globals["app_title"] = APP_TITLE_DEFAULT
templates.env.globals["app_version"] = _resolve_app_version()
templates.env.globals["app_logo"] = False          # bool: whether a logo is configured
templates.env.globals["app_logo_position"] = "left"
templates.env.globals["app_logo_size"] = "80"
templates.env.globals["app_logo_show_title"] = "true"
templates.env.globals["app_logo_title_size"] = "12"

# License / edition globals — set at startup by main.py lifespan. Default to
# Community so any early render before load_license() runs is safe.
templates.env.globals["edition"] = "community"
templates.env.globals["is_enterprise"] = False
templates.env.globals["license_info"] = None

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
    # Update notifier (CHANGELOG entry under "Added"). Hydrated here
    # so the base.html banner partial reads the latest snapshot via
    # Jinja env globals — avoids a per-render DB query.
    "updates.check_enabled", "updates.latest_version",
    "updates.latest_url", "updates.latest_published_at",
    "updates.checked_at", "updates.check_error",
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


def _normalise_version_tag(raw: str) -> tuple[int, ...]:
    """Parse ``v1.2.3`` / ``1.2.3-beta`` into a sortable tuple.

    Comparison uses only the numeric ``MAJOR.MINOR.PATCH`` prefix and
    drops pre-release / build suffixes (``-beta``, ``+sha.abc``). Bad
    inputs return ``()`` which sorts before any real version, so a
    parse failure on the *latest* tag means "no banner shown" rather
    than "false positive". Mirrors the conservative defaults of the
    ``updates.*`` config: stay quiet on uncertainty.
    """
    s = (raw or "").strip()
    if s.startswith(("v", "V")):
        s = s[1:]
    # Drop pre-release / build metadata before counting parts.
    for sep in ("-", "+"):
        if sep in s:
            s = s.split(sep, 1)[0]
    try:
        return tuple(int(p) for p in s.split(".") if p)
    except (TypeError, ValueError):
        return ()


def _compute_banner_state() -> dict:
    """Derive the update-banner viewmodel from current Jinja globals.

    Returns a dict with the fields the banner partial reads — keeps the
    template free of comparison logic. ``visible`` is the only field
    the banner conditionally renders on; the rest are display values.
    """
    g = templates.env.globals
    if not bool(g.get("updates_check_enabled", False)):
        return {"visible": False}
    latest = (g.get("updates_latest_version") or "").strip()
    if not latest:
        return {"visible": False}
    current = (g.get("app_version") or "").strip()
    if not current:
        return {"visible": False}
    # Outdated when the parsed-tuple comparison says so AND both parsed
    # cleanly. Falling through to "no banner" on either parse failure.
    cur_t = _normalise_version_tag(current)
    new_t = _normalise_version_tag(latest)
    if not cur_t or not new_t or new_t <= cur_t:
        return {"visible": False}
    return {
        "visible": True,
        "current": current,
        "latest": latest,
        "url": g.get("updates_latest_url") or "",
        "published_at": g.get("updates_latest_published_at") or "",
    }


def set_update_globals(rows: dict) -> None:
    """Push ``updates.*`` config rows into the Jinja env.

    ``rows`` is ``{key: value}`` from the refresh query — all values
    are strings (or ``""`` when unset). Stored as flat globals named
    ``updates_check_enabled`` / ``updates_latest_version`` / … so
    templates read them with predictable identifiers.
    """
    truthy = (rows.get("updates.check_enabled") or "false").strip().lower() in (
        "true", "1", "yes", "on", "enabled",
    )
    templates.env.globals["updates_check_enabled"] = truthy
    templates.env.globals["updates_latest_version"] = (rows.get("updates.latest_version") or "").strip()
    templates.env.globals["updates_latest_url"] = (rows.get("updates.latest_url") or "").strip()
    templates.env.globals["updates_latest_published_at"] = (rows.get("updates.latest_published_at") or "").strip()
    templates.env.globals["updates_checked_at"] = (rows.get("updates.checked_at") or "").strip()
    templates.env.globals["updates_check_error"] = (rows.get("updates.check_error") or "").strip()


# Initial defaults so templates rendered before the first refresh tick
# (e.g. the login page on a cold start) don't trip on missing keys.
templates.env.globals["updates_check_enabled"] = False
templates.env.globals["updates_latest_version"] = ""
templates.env.globals["updates_latest_url"] = ""
templates.env.globals["updates_latest_published_at"] = ""
templates.env.globals["updates_checked_at"] = ""
templates.env.globals["updates_check_error"] = ""
templates.env.globals["update_banner_state"] = _compute_banner_state


def set_license_globals(info) -> None:
    """Publish license fields to the Jinja2 environment.

    ``info`` is an ``app.utils.license.LicenseInfo``; accepting duck-typed input
    avoids a circular import at module load time.
    """
    if info is None:
        templates.env.globals["edition"] = "community"
        templates.env.globals["is_enterprise"] = False
        templates.env.globals["license_info"] = None
        return
    templates.env.globals["edition"] = info.edition
    templates.env.globals["is_enterprise"] = (info.edition == "enterprise" and info.valid)
    templates.env.globals["license_info"] = info


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
                update_rows: dict[str, str] = {}
                for cfg in rows.scalars().all():
                    seen.add(cfg.key)
                    if cfg.key == "app.title":
                        set_app_title(cfg.value)
                    elif cfg.key.startswith("updates."):
                        # Buffered — pushed once at the end so the banner
                        # state is derived from the full snapshot, not
                        # whatever ordering the SELECT returned.
                        update_rows[cfg.key] = cfg.value or ""
                    else:
                        set_app_logo_config(cfg.key, cfg.value)
                # Keys missing from the DB → reset to defaults so a removed
                # logo also reverts here (not just on the worker that wrote).
                if "app.logo" not in seen:
                    set_app_logo_config("app.logo", "")
                set_update_globals(update_rows)
            _last_config_refresh_ts = now
        except Exception:
            # Swallow: a transient DB hiccup must not break page renders.
            pass
