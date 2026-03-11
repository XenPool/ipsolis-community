"""Runbook: VDI Reclaim – return VM to pool and trigger SCCM reinstall.

Corresponds to the Ivanti runbook 'VDI Reclaim'.

Flow:
  1. Active Roles: clear all groups
  2. SCCM: trigger unattended reinstall
  3. Pool: set status to RECLAIMING
  4. Notification: send reclaim notification
  (5. After SCCM completes: set status to FREE – separate beat task)

Celery Beat Task: check_expiring_assets – checks hourly for expiring assets.
"""

import logging
import os
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tasks import app
from tasks.modules import active_roles, audit_helper, notifications, pool_manager, sccm
from tasks.modules.step_helper import update_order_step, update_order_status

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
    Runbook: Decommission VM and start SCCM reinstall.
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

        # ── Step 1/4: Clear AD groups ────────────────────────────────────
        step = "active_roles.remove_all_groups"
        logger.info("[Step 1/4] %s for '%s'", step, asset_name)
        update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
        try:
            result = active_roles.remove_all_groups(asset_name)
            if not result["success"]:
                raise RuntimeError(result.get("error"))
            update_order_step(db, order_id, step, "success",
                              log_output=str(result),
                              finished_at=datetime.now(timezone.utc))
        except Exception as e:
            update_order_step(db, order_id, step, "failed", error=str(e),
                              finished_at=datetime.now(timezone.utc))
            logger.warning("[Step 1/4] %s failed (non-critical): %s", step, e)

        # ── Step 2/4: Trigger SCCM reinstall ──────────────────────────────
        step = "sccm.trigger_reinstall"
        logger.info("[Step 2/4] %s for '%s'", step, asset_name)
        update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
        try:
            result = sccm.trigger_reinstall(asset_name)
            if not result["success"]:
                raise RuntimeError(f"SCCM trigger failed: {result.get('error')}")
            update_order_step(db, order_id, step, "success",
                              log_output=str(result),
                              finished_at=datetime.now(timezone.utc))
        except Exception as e:
            update_order_step(db, order_id, step, "failed", error=str(e),
                              finished_at=datetime.now(timezone.utc))
            update_order_status(db, order_id, "failed", str(e))
            audit_helper.waudit(
                db, "order", order_id, "status_changed",
                old={"status": "processing"}, new={"status": "failed", "error": str(e)},
                by="celery:vdi_reclaim", ctx=str(self.request.id),
            )
            db.commit()
            logger.error("[Step 2/4] FAILED: %s", e)
            raise self.retry(exc=e)

        # ── Step 3/4: Set asset to RECLAIMING ──────────────────────────────
        step = "pool.set_reclaiming"
        logger.info("[Step 3/4] %s asset_id=%s", step, asset_id)
        update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
        try:
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
                audit_helper.waudit(
                    db, "asset", asset_id, "status_changed",
                    old={"status": "busy", "current_order_id": order_id},
                    new={"status": "reclaiming", "current_order_id": None},
                    by="celery:vdi_reclaim", ctx=str(order_id),
                )
                db.commit()
                update_order_step(db, order_id, step, "success",
                                  log_output=f"Asset {asset_id} set to reclaiming",
                                  finished_at=datetime.now(timezone.utc))
            else:
                update_order_step(db, order_id, step, "skipped",
                                  log_output="Skipped: no assigned_asset_id",
                                  finished_at=datetime.now(timezone.utc))
        except Exception as e:
            update_order_step(db, order_id, step, "failed", error=str(e),
                              finished_at=datetime.now(timezone.utc))
            update_order_status(db, order_id, "failed", str(e))
            audit_helper.waudit(
                db, "order", order_id, "status_changed",
                old={"status": "processing"}, new={"status": "failed", "error": str(e)},
                by="celery:vdi_reclaim", ctx=str(self.request.id),
            )
            db.commit()
            logger.error("[Step 3/4] FAILED: %s", e)
            raise self.retry(exc=e)

        # ── Step 4/4: Notification ─────────────────────────────────────
        step = "notifications.send_reclaim_notification"
        logger.info("[Step 4/4] %s to %s", step, order["user_email"])
        update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
        try:
            notifications.send_reclaim_notification(
                user_email=order["user_email"],
                user_name=order["user_name"],
                asset_name=asset_name,
            )
            update_order_step(db, order_id, step, "success",
                              finished_at=datetime.now(timezone.utc))
        except Exception as e:
            logger.warning("[Step 4/4] Notification failed (non-critical): %s", e)
            update_order_step(db, order_id, step, "failed", error=str(e),
                              finished_at=datetime.now(timezone.utc))

        # ── Set order to EXPIRED ───────────────────────────────────────────
        update_order_status(db, order_id, "expired")
        audit_helper.waudit(
            db, "order", order_id, "status_changed",
            old={"status": "delivered"}, new={"status": "expired"},
            by="celery:vdi_reclaim",
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
        audit_helper.waudit(
            db, "order", order_id, "status_changed",
            old={"status": "processing"}, new={"status": "failed", "error": str(e)},
            by="celery:vdi_reclaim",
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
    Celery Beat Task: Checks hourly for expiring assets and triggers reclaim.

    - Immediately expired (expires_at <= NOW): start reclaim runbook
    - Expiring soon (expires_at <= NOW + REMINDER_HOURS): send reminder email
    """
    from datetime import timedelta

    reminder_hours = int(os.getenv("REMINDER_HOURS_BEFORE_EXPIRY", "24"))

    logger.info("=== check_expiring_assets: Checking for expired/expiring assets ===")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    db = Session(engine)

    try:
        # Expired assets (reclaim immediately)
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

        # Assets expiring soon (reminder email)
        reminder_time = datetime.now(timezone.utc) + timedelta(hours=reminder_hours)
        expiring_soon = db.execute(
            text("""
                SELECT a.name as asset_name, o.user_email, o.user_name, a.expires_at
                FROM asset_pool a
                JOIN orders o ON o.id = a.current_order_id
                WHERE a.status = 'busy'
                  AND a.expires_at > NOW()
                  AND a.expires_at <= :reminder_time
                  AND o.status IN ('delivered', 'provisioned')
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
