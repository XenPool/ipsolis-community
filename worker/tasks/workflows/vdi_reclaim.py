"""Runbook: VDI Reclaim – VM zurückführen und SCCM Reinstall triggern.

Entspricht dem Ivanti-Runbook 'VDI Rückführen'.

Ablauf:
  1. Active Roles: Alle Gruppen leeren
  2. SCCM: Unattended Reinstall triggern
  3. Pool: Status auf RECLAIMING setzen
  4. Notification: Rückgabe-Benachrichtigung senden
  (5. Nach SCCM-Abschluss: Status auf FREE – separater Beat-Task)

Celery Beat Task: check_expiring_assets – prüft stündlich ablaufende Assets.
"""

import logging
import os
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tasks import app
from tasks.modules import active_roles, notifications, pool_manager, sccm

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://xpuser:changeme@localhost:5432/itselfservice",
).replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@app.task(
    name="tasks.workflows.vdi_reclaim.run",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    queue="reclaim",
)
def run(self: Task, order_id: int) -> dict:
    """
    Runbook: VM aus dem Betrieb nehmen und SCCM-Reinstall starten.
    """
    logger.info("=== vdi_reclaim START: order_id=%s ===", order_id)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    db = Session(engine)

    try:
        row = db.execute(
            text("""
                SELECT o.id, o.user_email, o.user_name, o.assigned_asset_id,
                       a.name as asset_name
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
        asset_id = order.get("assigned_asset_id")

        # Step 1: AD-Gruppen leeren
        logger.info("[Step 1/4] active_roles.remove_all_groups for '%s'", asset_name)
        result = active_roles.remove_all_groups(asset_name)
        if not result["success"]:
            logger.warning("[Step 1/4] Warning: %s", result.get("error"))

        # Step 2: SCCM Reinstall triggern
        logger.info("[Step 2/4] sccm.trigger_reinstall for '%s'", asset_name)
        result = sccm.trigger_reinstall(asset_name)
        if not result["success"]:
            raise RuntimeError(f"SCCM trigger failed: {result.get('error')}")

        # Step 3: Asset auf RECLAIMING setzen
        logger.info("[Step 3/4] Setting asset to RECLAIMING")
        if asset_id:
            db.execute(
                text("""
                    UPDATE asset_pool
                    SET status = 'reclaiming', current_order_id = NULL,
                        expires_at = NULL, last_reclaim_at = NOW(), updated_at = NOW()
                    WHERE id = :asset_id
                """),
                {"asset_id": asset_id},
            )
            db.commit()

        # Step 4: Benachrichtigung
        logger.info("[Step 4/4] notifications.send_reclaim_notification")
        notifications.send_reclaim_notification(
            user_email=order["user_email"],
            user_name=order["user_name"],
            asset_name=asset_name,
        )

        # Order auf EXPIRED setzen
        db.execute(
            text("UPDATE orders SET status = 'expired', updated_at = NOW() WHERE id = :id"),
            {"id": order_id},
        )
        db.commit()

        logger.info(
            "=== vdi_reclaim COMPLETE: order_id=%s asset=%s (SCCM reinstall in progress) ===",
            order_id, asset_name,
        )
        return {"success": True, "order_id": order_id, "asset_name": asset_name}

    except Exception as e:
        logger.error("=== vdi_reclaim FAILED: order_id=%s error=%s ===", order_id, e)
        db.execute(
            text("UPDATE orders SET status = 'failed', error_message = :err, updated_at = NOW() WHERE id = :id"),
            {"err": str(e), "id": order_id},
        )
        db.commit()
        raise self.retry(exc=e)
    finally:
        db.close()


@app.task(
    name="tasks.workflows.vdi_reclaim.check_expiring_assets",
    queue="reclaim",
)
def check_expiring_assets() -> dict:
    """
    Celery Beat Task: Prüft stündlich ablaufende Assets und triggert Reclaim.

    - Sofortiger Ablauf (expires_at <= NOW): Reclaim-Runbook starten
    - Bald ablaufend (expires_at <= NOW + REMINDER_HOURS): Erinnerungsmail
    """
    from datetime import timedelta

    reminder_hours = int(os.getenv("REMINDER_HOURS_BEFORE_EXPIRY", "24"))

    logger.info("=== check_expiring_assets: Checking for expired/expiring assets ===")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    db = Session(engine)

    try:
        # Abgelaufene Assets (sofort reclaimen)
        expired = db.execute(
            text("""
                SELECT a.id as asset_id, a.name as asset_name, o.id as order_id,
                       o.user_email, o.user_name, a.expires_at
                FROM asset_pool a
                JOIN orders o ON o.id = a.current_order_id
                WHERE a.status = 'busy'
                  AND a.expires_at <= NOW()
                  AND o.status NOT IN ('expired', 'cancelled', 'failed')
            """),
        ).fetchall()

        reclaim_count = 0
        for row in expired:
            logger.info(
                "Asset expired: %s (order_id=%s, expired_at=%s) – triggering reclaim",
                row.asset_name, row.order_id, row.expires_at,
            )
            run.delay(row.order_id)
            reclaim_count += 1

        # Bald ablaufende Assets (Erinnerungsmail)
        reminder_time = datetime.now(timezone.utc) + timedelta(hours=reminder_hours)
        expiring_soon = db.execute(
            text("""
                SELECT a.name as asset_name, o.user_email, o.user_name, a.expires_at
                FROM asset_pool a
                JOIN orders o ON o.id = a.current_order_id
                WHERE a.status = 'busy'
                  AND a.expires_at > NOW()
                  AND a.expires_at <= :reminder_time
                  AND o.status = 'delivered'
            """),
            {"reminder_time": reminder_time},
        ).fetchall()

        reminder_count = 0
        for row in expiring_soon:
            hours_remaining = (
                row.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
            ).total_seconds() / 3600
            logger.info(
                "Asset expiring soon: %s (user=%s, in %.1fh) – sending reminder",
                row.asset_name, row.user_email, hours_remaining,
            )
            notifications.send_expiry_reminder(
                user_email=row.user_email,
                user_name=row.user_name,
                asset_name=row.asset_name,
                expires_at=row.expires_at,
                hours_remaining=hours_remaining,
            )
            reminder_count += 1

        logger.info(
            "=== check_expiring_assets DONE: %s reclaimed, %s reminders sent ===",
            reclaim_count, reminder_count,
        )
        return {"reclaimed": reclaim_count, "reminders_sent": reminder_count}

    finally:
        db.close()
