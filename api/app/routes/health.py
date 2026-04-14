import os

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(tags=["health"])

_VERSION = os.environ.get("APP_VERSION", "0.0.0")


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)) -> dict:
    """Liveness + Readiness Check."""
    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unavailable",
        "version": _VERSION,
        "service": "xp-api",
    }
