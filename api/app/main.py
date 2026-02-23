import logging

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.app.config import settings
from api.app.routes import assets, health, orders, webhook

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

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="XenPool IT Selfservice API",
    description=(
        "Dispatcher und REST-API für den eigenständigen Ivanti-Automation-Ersatz. "
        "Empfängt Webhooks von ServiceNow und Self-Service-Portal-Anfragen, "
        "legt Orders an und dispatcht Celery-Runbooks."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(orders.router)
app.include_router(assets.router)


# ── Startup / Shutdown ────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    logger.info(
        "XenPool IT Selfservice API starting",
        environment=settings.ENVIRONMENT,
        version="0.1.0",
    )
    if settings.is_development:
        logger.info("Mock mode ACTIVE – no real external calls will be made")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("XenPool IT Selfservice API shutting down")
