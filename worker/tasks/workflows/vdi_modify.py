"""Runbook: VDI Modify – Benutzer ändern oder Laufzeit verlängern.

Entspricht dem Ivanti-Runbook 'VDI Ändern / Verlängern'.

Ablauf:
  1. Active Roles: RDP-Gruppe aktualisieren (wenn geändert)
  2. Active Roles: Admin-Gruppe aktualisieren (wenn geändert)
  3. Pool: Ablaufzeit aktualisieren (wenn verlängert)
  4. Notification: Änderungsbestätigung senden
"""

import logging
import os
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tasks import app
from tasks.modules import active_roles, notifications, pool_manager

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://xpuser:changeme@localhost:5432/itselfservice",
).replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@app.task(
    name="tasks.workflows.vdi_modify.run",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue="provision",
)
def run(self: Task, order_id: int) -> dict:
    """
    Runbook: VDI-Einstellungen ändern (User, Laufzeit).
    """
    logger.info("=== vdi_modify START: order_id=%s ===", order_id)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    db = Session(engine)

    try:
        from sqlalchemy import text
        row = db.execute(
            text("""
                SELECT o.id, o.user_email, o.user_name, o.rdp_users, o.admin_users,
                       o.requested_until, o.assigned_asset_id, a.name as asset_name
                FROM orders o
                LEFT JOIN asset_pool a ON a.id = o.assigned_asset_id
                WHERE o.id = :order_id
            """),
            {"order_id": order_id},
        ).fetchone()

        if not row:
            raise ValueError(f"Order {order_id} not found")

        order = row._asdict()
        asset_name = order.get("asset_name") or f"VDI-MOCK-{order_id}"

        logger.info("[Step 1/4] active_roles.set_rdp_group")
        active_roles.set_rdp_group(asset_name, order["rdp_users"] or [])

        logger.info("[Step 2/4] active_roles.set_admin_group")
        active_roles.set_admin_group(asset_name, order["admin_users"] or [])

        logger.info("[Step 3/4] pool_manager: Updating expires_at")
        expires_at = order["requested_until"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if order.get("assigned_asset_id"):
            pool_manager.set_asset_busy(db, order["assigned_asset_id"], order_id, expires_at)

        logger.info("[Step 4/4] notifications")
        notifications.send_provision_confirmation(
            user_email=order["user_email"],
            user_name=order["user_name"],
            asset_name=asset_name,
            rdp_users=order["rdp_users"] or [],
            expires_at=expires_at,
        )

        db.execute(
            text("UPDATE orders SET status = 'delivered', updated_at = NOW() WHERE id = :id"),
            {"id": order_id},
        )
        db.commit()

        logger.info("=== vdi_modify COMPLETE: order_id=%s ===", order_id)
        return {"success": True, "order_id": order_id}

    except Exception as e:
        logger.error("=== vdi_modify FAILED: order_id=%s error=%s ===", order_id, e)
        from sqlalchemy import text
        db.execute(
            text("UPDATE orders SET status = 'failed', error_message = :err, updated_at = NOW() WHERE id = :id"),
            {"err": str(e), "id": order_id},
        )
        db.commit()
        raise self.retry(exc=e)
    finally:
        db.close()
