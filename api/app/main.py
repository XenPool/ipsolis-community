import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.config import AppConfig
from app.routes import admin, admin_api_tokens, admin_auth, admin_cost_report, admin_license, admin_maintenance, admin_modules, admin_runbooks, admin_seed_export, admin_standalone_runbooks, approvals_external, assets, auth, health, metrics as metrics_route, orders, portal, ui, webhook
from app.utils import metrics as metrics_util
from app.templates_instance import set_app_title, set_app_logo_config, set_license_globals, refresh_app_config_if_stale
from app.utils.license import load_license

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)

logger = structlog.get_logger(__name__)

APP_VERSION = os.environ.get("APP_VERSION", "0.0.0")


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Application starting", version=APP_VERSION)

    # Load configurable app globals from DB into Jinja2 environment
    _APP_KEYS = ("app.title", "app.logo", "app.logo_position", "app.logo_size", "app.logo_show_title", "app.logo_title_size")
    try:
        async with AsyncSessionLocal() as db:
            rows = await db.execute(
                select(AppConfig).where(AppConfig.key.in_(_APP_KEYS))
            )
            for cfg in rows.scalars().all():
                if cfg.key == "app.title":
                    set_app_title(cfg.value)
                else:
                    set_app_logo_config(cfg.key, cfg.value)
    except Exception as exc:
        logger.warning("Could not load app config globals from DB at startup: %s", exc)

    # Load license and publish edition globals to Jinja2 (safe on error)
    try:
        license_info = load_license()
        set_license_globals(license_info)
        logger.info(
            "License loaded", edition=license_info.edition,
            valid=license_info.valid, licensee=license_info.licensee,
        )
    except Exception as exc:
        logger.warning("License load failed; running Community: %s", exc)

    yield
    logger.info("Application shutting down")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Ipsolis API",
    description=(
        "Dispatcher and REST API for IT asset lifecycle orchestration. "
        "Receives webhooks from ServiceNow and self-service portal requests, "
        "creates orders and dispatches Celery runbooks."
    ),
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Session (must be added before CORS so the cookie is available in all routes)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.API_SECRET_KEY,
    session_cookie="xp_session",
    max_age=28800,       # 8 hours
    https_only=True,
    same_site="lax",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── App-config refresh (per-worker TTL, keeps multi-worker setups in sync) ────
_CONFIG_SYNC_PATHS = ("/ui", "/portal", "/admin")


@app.middleware("http")
async def sync_app_config_globals(request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in _CONFIG_SYNC_PATHS):
        await refresh_app_config_if_stale()
    return await call_next(request)


# ── Prometheus request metrics ────────────────────────────────────────────────
import time as _time  # noqa: E402 — local to the middleware


@app.middleware("http")
async def record_request_metrics(request, call_next):
    """Record request count + latency, labelled by route template."""
    started = _time.perf_counter()
    response = await call_next(request)
    duration = _time.perf_counter() - started

    path = request.url.path
    # /metrics scrapes don't count toward themselves to avoid trivially
    # inflating the request rate displayed on dashboards.
    if path == "/metrics":
        return response

    bucketed = metrics_util.collapse_high_volume_paths(path)
    if bucketed is not None:
        route_label = bucketed
    else:
        route = request.scope.get("route")
        route_label = metrics_util.safe_route_template(
            getattr(route, "path", None), fallback="<unmatched>"
        )

    metrics_util.record_request(
        method=request.method,
        route=route_label,
        status_code=response.status_code,
        duration_seconds=duration,
    )
    return response


# ── Static Files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")

# Portal i18n locale files (served as static JSON, fetched by /static/js/i18n.js)
_LOCALES_DIR = "/app/locales"
if os.path.isdir(_LOCALES_DIR):
    app.mount("/locales", StaticFiles(directory=_LOCALES_DIR), name="locales")
else:
    logger.warning("Locales directory not found at %s — portal i18n will use fallback keys", _LOCALES_DIR)

# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(orders.router)
app.include_router(assets.router)
app.include_router(admin.router)
app.include_router(admin_modules.router)
app.include_router(admin_runbooks.router)
app.include_router(admin_standalone_runbooks.router)
app.include_router(admin_maintenance.router)
app.include_router(admin_license.router)
app.include_router(admin_api_tokens.router)
app.include_router(admin_cost_report.router)
app.include_router(admin_seed_export.router)
app.include_router(admin_auth.router)  # admin login/logout — no auth, before ui.router
app.include_router(ui.router)
app.include_router(auth.router)   # login / callback / logout — before portal
app.include_router(portal.router)
app.include_router(approvals_external.router)  # tokenized /approve/{token} (no auth required)
app.include_router(metrics_route.router)        # /metrics (Prometheus)
