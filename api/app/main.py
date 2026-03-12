import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes import admin, admin_modules, admin_runbooks, assets, health, orders, portal, ui, webhook

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.is_development else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.DEBUG if settings.is_development else logging.INFO
    ),
)

logger = structlog.get_logger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(
        "XenPool IT Selfservice API starting",
        environment=settings.ENVIRONMENT,
        version="0.1.0",
    )
    if settings.is_development:
        logger.info("Mock mode ACTIVE – no real external calls will be made")
    yield
    logger.info("XenPool IT Selfservice API shutting down")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="XenPool IT Selfservice API",
    description=(
        "Dispatcher and REST API for the standalone Ivanti Automation replacement. "
        "Receives webhooks from ServiceNow and self-service portal requests, "
        "creates orders and dispatches Celery runbooks."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
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
app.include_router(ui.router)
app.include_router(portal.router)
