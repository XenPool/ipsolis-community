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
from app.routes import admin, admin_auth, admin_modules, admin_runbooks, admin_standalone_runbooks, assets, auth, health, orders, portal, ui, webhook
from app.templates_instance import set_app_title, set_app_logo_config

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
    logger.info("IT Selfservice API starting", version=APP_VERSION)

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

    yield
    logger.info("IT Selfservice API shutting down")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="IT Selfservice API",
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

# ── Static Files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")

# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(orders.router)
app.include_router(assets.router)
app.include_router(admin.router)
app.include_router(admin_modules.router)
app.include_router(admin_runbooks.router)
app.include_router(admin_standalone_runbooks.router)
app.include_router(admin_auth.router)  # admin login/logout — no auth, before ui.router
app.include_router(ui.router)
app.include_router(auth.router)   # login / callback / logout — before portal
app.include_router(portal.router)
