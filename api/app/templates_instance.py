"""Shared Jinja2Templates instance used by all route modules.

Centralising the instance allows Jinja2 environment globals (e.g. app_title,
app_logo) to be set once and reflected in every template render without
per-request DB queries or per-route parameter passing.
"""

import os

from fastapi.templating import Jinja2Templates

APP_TITLE_DEFAULT = "IT Selfservice"

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
