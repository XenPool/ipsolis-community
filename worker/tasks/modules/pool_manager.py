"""Modul: Pool Manager – VM aus Pool wählen und zurückgeben.

Entspricht dem Ivanti-Modul 'Pool Management'.
"""

import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
MOCK_SCRIPTS = os.getenv("MOCK_SCRIPTS", "true" if ENVIRONMENT == "development" else "false").lower() == "true"


def reserve_asset(
    db: Session,
    order_id: int,
    asset_type_id: int,
    expires_at: datetime,
    personal_provisioning_strategy: str = "assign_existing_free",
    user_email: str | None = None,
) -> dict:
    """
    Reserviert eine VM passender Ausprägung gemäß personal_provisioning_strategy.

    Strategien:
        assign_existing_free      – erste freie Instanz aus Pool (Standardverhalten)
        reuse_existing_by_owner   – bereits zugewiesene Instanz des Users wiederverwenden
        create_new                – neue Instanz anlegen (Stub; echte Provision per Runbook)

    Returns:
        {"success": True, "asset_id": int, "asset_name": str}
        {"success": False, "error": str}
    """
    if MOCK_SCRIPTS:
        return _mock_reserve_asset(db, order_id, asset_type_id, expires_at)

    # REUSE_EXISTING_BY_OWNER: User hat bereits eine zugewiesene Instanz
    if personal_provisioning_strategy == "reuse_existing_by_owner" and user_email:
        row = db.execute(
            sql_text("""
                SELECT id, name FROM asset_pool
                WHERE asset_type_id = :at
                  AND status IN ('busy', 'reserved')
                  AND metadata->>'owner_email' = :email
                LIMIT 1
            """),
            {"at": asset_type_id, "email": user_email},
        ).fetchone()
        if row:
            logger.info("Reusing existing asset id=%s for user=%s", row[0], user_email)
            return {"success": True, "asset_id": row[0], "asset_name": row[1], "reused": True}
        # Kein vorhandenes Asset → fallback auf assign_existing_free
        logger.info("No existing asset found for user=%s – falling back to assign_existing_free", user_email)

    # CREATE_NEW: Stub – echte Instanzerstellung über Runbook-Step
    if personal_provisioning_strategy == "create_new":
        logger.info(
            "[STUB] create_new: Neue Instanz für order_id=%s asset_type_id=%s – "
            "Echte Erstellung muss über vsphere-Runbook erfolgen", order_id, asset_type_id,
        )
        return {
            "success": True,
            "asset_id": None,
            "asset_name": f"NEW-INSTANCE-order-{order_id}",
            "stub": True,
        }

    # ASSIGN_EXISTING_FREE (Default): Erste freie Instanz aus Pool – Race-Condition-safe
    row = db.execute(
        sql_text("""
            SELECT id, name, metadata FROM asset_pool
            WHERE asset_type_id = :at AND status = 'free'
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        """),
        {"at": asset_type_id},
    ).fetchone()

    if not row:
        return {"success": False, "error": f"No free asset available for type {asset_type_id}"}

    asset_id, asset_name, metadata = row[0], row[1], row[2] or {}
    if user_email:
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        metadata = {**metadata, "owner_email": user_email}

    db.execute(
        sql_text("""
            UPDATE asset_pool
            SET status = 'reserved',
                current_order_id = :order_id,
                expires_at = :expires_at,
                metadata = CAST(:metadata AS jsonb)
            WHERE id = :id
        """),
        {
            "id": asset_id,
            "order_id": order_id,
            "expires_at": expires_at,
            "metadata": json.dumps(metadata),
        },
    )
    db.execute(
        sql_text("UPDATE orders SET assigned_asset_id = :aid WHERE id = :oid"),
        {"aid": asset_id, "oid": order_id},
    )
    db.commit()

    logger.info("Asset reserved: asset_id=%s order_id=%s owner=%s", asset_id, order_id, user_email)
    return {"success": True, "asset_id": asset_id, "asset_name": asset_name}


def release_asset(db: Session, asset_id: int) -> dict:
    """Gibt eine VM zurück in den Pool (Status FREE).
    Kein Mock: reine DB-Operation, immer ausführen."""
    result = db.execute(
        sql_text("""
            UPDATE asset_pool
            SET status = 'free',
                current_order_id = NULL,
                expires_at = NULL,
                last_reclaim_at = :now
            WHERE id = :id
        """),
        {"id": asset_id, "now": datetime.now(timezone.utc)},
    )
    if result.rowcount == 0:
        return {"success": False, "error": f"Asset {asset_id} not found"}
    db.commit()

    logger.info("Asset released: asset_id=%s", asset_id)
    return {"success": True, "asset_id": asset_id}


def set_asset_busy(db: Session, asset_id: int, order_id: int, expires_at: datetime) -> dict:
    """Setzt VM auf BUSY nach erfolgreicher Bereitstellung."""
    # Kein Mock: reine DB-Operation, immer ausführen
    result = db.execute(
        sql_text("""
            UPDATE asset_pool
            SET status = 'busy',
                current_order_id = :order_id,
                expires_at = :expires_at
            WHERE id = :id
        """),
        {"id": asset_id, "order_id": order_id, "expires_at": expires_at},
    )
    if result.rowcount == 0:
        return {"success": False, "error": f"Asset {asset_id} not found"}
    db.commit()
    return {"success": True}


def check_capacity(db: Session, asset_type_id: int, pool_capacity: int) -> dict:
    """Prüft ob Pool-Kapazität für pooled Assets noch frei ist.

    Returns:
        {"success": True, "current": n, "capacity": m}
        {"success": False, "current": n, "capacity": m, "error": str}
    """
    if MOCK_SCRIPTS:
        logger.info(
            "[MOCK] check_capacity: asset_type_id=%s capacity=%s",
            asset_type_id, pool_capacity,
        )
        return {"success": True, "current": 3, "capacity": pool_capacity, "mock": True}

    row = db.execute(
        sql_text("""
            SELECT COUNT(*) FROM orders
            WHERE asset_type_id = :at AND status IN ('processing', 'delivered')
        """),
        {"at": asset_type_id},
    ).fetchone()
    current = row[0] if row else 0
    if current >= pool_capacity:
        return {
            "success": False,
            "current": current,
            "capacity": pool_capacity,
            "error": f"Pool capacity reached ({current}/{pool_capacity})",
        }
    return {"success": True, "current": current, "capacity": pool_capacity}


# ── Mocks ─────────────────────────────────────────────────────────────────────

def _mock_reserve_asset(db: Session, order_id: int, asset_type_id: int, expires_at: datetime) -> dict:
    import time
    logger.info(
        "[MOCK] Searching pool for asset_type_id=%s order_id=%s ...",
        asset_type_id, order_id,
    )
    time.sleep(0.5)  # Simuliert DB-Zugriff

    # Versuche ein echtes freies Asset zu nehmen, damit assigned_asset_id auf der Order stimmt
    row = db.execute(
        sql_text("""
            SELECT id, name FROM asset_pool
            WHERE asset_type_id = :at AND status = 'free'
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        """),
        {"at": asset_type_id},
    ).fetchone()

    if row:
        asset_id, asset_name = row[0], row[1]
        db.execute(
            sql_text("""
                UPDATE asset_pool
                SET status = 'reserved',
                    current_order_id = :order_id,
                    expires_at = :expires_at
                WHERE id = :id
            """),
            {"id": asset_id, "order_id": order_id, "expires_at": expires_at},
        )
        db.execute(
            sql_text("UPDATE orders SET assigned_asset_id = :aid WHERE id = :oid"),
            {"aid": asset_id, "oid": order_id},
        )
        db.commit()
        logger.info(
            "[MOCK] Asset reserved: %s (id=%s) for order %s until %s",
            asset_name, asset_id, order_id, expires_at.isoformat(),
        )
        return {"success": True, "asset_id": asset_id, "asset_name": asset_name}

    # Kein echtes Asset im Pool – Fallback auf Mock-ID (Development ohne befüllten Pool)
    mock_asset_id = 1000 + order_id
    mock_asset_name = f"VDI-MOCK-{mock_asset_id:04d}"
    logger.info(
        "[MOCK] No real asset found – using mock: %s (id=%s) for order %s",
        mock_asset_name, mock_asset_id, order_id,
    )
    return {"success": True, "asset_id": mock_asset_id, "asset_name": mock_asset_name}


def _mock_release_asset(asset_id: int) -> dict:
    import time
    logger.info("[MOCK] Releasing asset_id=%s back to pool ...", asset_id)
    time.sleep(0.3)
    logger.info("[MOCK] Asset %s is FREE", asset_id)
    return {"success": True, "asset_id": asset_id}
