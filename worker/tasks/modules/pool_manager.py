"""Modul: Pool Manager – VM aus Pool wählen und zurückgeben.

Entspricht dem Ivanti-Modul 'Pool Management'.
"""

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")


def reserve_asset(
    db: Session,
    order_id: int,
    asset_type_id: int,
    expires_at: datetime,
) -> dict:
    """
    Reserviert die erste freie VM passender Ausprägung.

    Returns:
        {"success": True, "asset_id": int, "asset_name": str}
        {"success": False, "error": str}
    """
    if ENVIRONMENT == "development":
        return _mock_reserve_asset(order_id, asset_type_id, expires_at)

    # Production: Echte DB-Abfrage
    from worker.models import AssetPool, AssetStatus  # lazy import

    asset = db.execute(
        select(AssetPool)
        .where(
            AssetPool.asset_type_id == asset_type_id,
            AssetPool.status == AssetStatus.FREE,
        )
        .with_for_update(skip_locked=True)  # Race-Condition-safe
        .limit(1)
    ).scalar_one_or_none()

    if not asset:
        return {"success": False, "error": f"No free asset available for type {asset_type_id}"}

    asset.status = AssetStatus.RESERVED
    asset.current_order_id = order_id
    asset.expires_at = expires_at
    db.commit()

    logger.info("Asset reserved: asset_id=%s order_id=%s", asset.id, order_id)
    return {"success": True, "asset_id": asset.id, "asset_name": asset.name}


def release_asset(db: Session, asset_id: int) -> dict:
    """Gibt eine VM zurück in den Pool (Status FREE)."""
    if ENVIRONMENT == "development":
        return _mock_release_asset(asset_id)

    from worker.models import AssetPool, AssetStatus

    asset = db.get(AssetPool, asset_id)
    if not asset:
        return {"success": False, "error": f"Asset {asset_id} not found"}

    asset.status = AssetStatus.FREE
    asset.current_order_id = None
    asset.expires_at = None
    asset.last_reclaim_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("Asset released: asset_id=%s", asset_id)
    return {"success": True, "asset_id": asset_id}


def set_asset_busy(db: Session, asset_id: int, order_id: int, expires_at: datetime) -> dict:
    """Setzt VM auf BUSY nach erfolgreicher Bereitstellung."""
    if ENVIRONMENT == "development":
        logger.info("[MOCK] set_asset_busy: asset_id=%s order_id=%s", asset_id, order_id)
        return {"success": True}

    from worker.models import AssetPool, AssetStatus

    asset = db.get(AssetPool, asset_id)
    if not asset:
        return {"success": False, "error": f"Asset {asset_id} not found"}

    asset.status = AssetStatus.BUSY
    asset.current_order_id = order_id
    asset.expires_at = expires_at
    db.commit()
    return {"success": True}


# ── Mocks ─────────────────────────────────────────────────────────────────────

def _mock_reserve_asset(order_id: int, asset_type_id: int, expires_at: datetime) -> dict:
    import time
    logger.info(
        "[MOCK] Searching pool for asset_type_id=%s order_id=%s ...",
        asset_type_id, order_id,
    )
    time.sleep(0.5)  # Simuliert DB-Zugriff
    mock_asset_id = 1000 + order_id
    mock_asset_name = f"VDI-MOCK-{mock_asset_id:04d}"
    logger.info(
        "[MOCK] Asset reserved: %s (id=%s) for order %s until %s",
        mock_asset_name, mock_asset_id, order_id, expires_at.isoformat(),
    )
    return {"success": True, "asset_id": mock_asset_id, "asset_name": mock_asset_name}


def _mock_release_asset(asset_id: int) -> dict:
    import time
    logger.info("[MOCK] Releasing asset_id=%s back to pool ...", asset_id)
    time.sleep(0.3)
    logger.info("[MOCK] Asset %s is FREE", asset_id)
    return {"success": True, "asset_id": asset_id}
