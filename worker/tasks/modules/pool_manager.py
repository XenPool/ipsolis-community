"""Module: Pool Manager – select VM from pool and return it.

Entspricht dem Ivanti-Modul 'Pool Management'.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

def reserve_asset(
    db: Session,
    order_id: int,
    asset_type_id: int,
    expires_at: datetime,
    personal_provisioning_strategy: str = "assign_existing_free",
    user_email: str | None = None,
) -> dict:
    """
    Reserves a VM of the appropriate type according to personal_provisioning_strategy.

    Strategien:
        assign_existing_free      – erste freie Instanz aus Pool (Standardverhalten)
        reuse_existing_by_owner   – bereits zugewiesene Instanz des Users wiederverwenden
        create_new                – neue Instanz anlegen (Stub; echte Provision per Runbook)

    Returns:
        {"success": True, "asset_id": int, "asset_name": str}
        {"success": False, "error": str}
    """
    # REUSE_EXISTING_BY_OWNER: user already has an assigned instance
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
        # No existing asset → fall back to assign_existing_free
        logger.info("No existing asset found for user=%s – falling back to assign_existing_free", user_email)

    # CREATE_NEW: stub – actual instance creation via runbook step
    if personal_provisioning_strategy == "create_new":
        logger.info(
            "[STUB] create_new: New instance for order_id=%s asset_type_id=%s – "
            "Actual creation must be performed via vsphere runbook", order_id, asset_type_id,
        )
        return {
            "success": True,
            "asset_id": None,
            "asset_name": f"NEW-INSTANCE-order-{order_id}",
            "stub": True,
        }

    # ASSIGN_EXISTING_FREE (default): first free instance from pool – race-condition-safe
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
    """Returns a VM to the pool (status FREE).
    No mock: pure DB operation, always execute."""
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
    # No mock: pure DB operation, always execute
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
    """Checks whether pool capacity for pooled assets is still available.

    Returns:
        {"success": True, "current": n, "capacity": m}
        {"success": False, "current": n, "capacity": m, "error": str}
    """
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

