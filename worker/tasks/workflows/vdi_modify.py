"""Runbook: VDI Modify – update users or extend duration.

Corresponds to the Ivanti runbook 'VDI Modify / Extend'.

Flow:
  1. Active Roles: update RDP group   (SKIPPED for action=extend)
  2. Active Roles: update admin group  (SKIPPED for action=extend)
  3. Pool: update expiry time          (SKIPPED if no requested_until)
  4. Notification: send change confirmation
"""

import logging
import os
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tasks import app
from tasks.modules import active_roles, audit_helper, notifications, pool_manager
from tasks.modules.step_helper import update_order_step, update_order_status

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
    Runbook: Modify VDI settings (users, duration).
    """
    logger.info("=== vdi_modify START: order_id=%s ===", order_id)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    db = Session(engine)

    try:
        row = db.execute(
            text("""
                SELECT o.id, o.user_email, o.user_name, o.rdp_users, o.admin_users,
                       o.requested_until, o.assigned_asset_id, o.action,
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
        action = (order.get("action") or "modify").lower()
        is_extend_only = action == "extend"

        expires_at = order["requested_until"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)

        # ── Step 1/4: Active Roles – RDP group ────────────────────────────
        step = "active_roles.set_rdp_group"
        logger.info("[Step 1/4] %s (action=%s)", step, action)
        update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
        try:
            if is_extend_only:
                update_order_step(db, order_id, step, "skipped",
                                  log_output="Skipped: action=extend",
                                  finished_at=datetime.now(timezone.utc))
            else:
                result = active_roles.set_rdp_group(asset_name, order["rdp_users"] or [])
                if not result["success"]:
                    raise RuntimeError(result["error"])
                update_order_step(db, order_id, step, "success",
                                  log_output=str(result),
                                  finished_at=datetime.now(timezone.utc))
        except Exception as e:
            update_order_step(db, order_id, step, "failed", error=str(e),
                              finished_at=datetime.now(timezone.utc))
            logger.warning("[Step 1/4] %s failed (non-critical): %s", step, e)

        # ── Step 2/4: Active Roles – admin group ──────────────────────────
        step = "active_roles.set_admin_group"
        logger.info("[Step 2/4] %s (action=%s)", step, action)
        update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
        try:
            if is_extend_only:
                update_order_step(db, order_id, step, "skipped",
                                  log_output="Skipped: action=extend",
                                  finished_at=datetime.now(timezone.utc))
            else:
                result = active_roles.set_admin_group(asset_name, order["admin_users"] or [])
                if not result["success"]:
                    raise RuntimeError(result["error"])
                update_order_step(db, order_id, step, "success",
                                  log_output=str(result),
                                  finished_at=datetime.now(timezone.utc))
        except Exception as e:
            update_order_step(db, order_id, step, "failed", error=str(e),
                              finished_at=datetime.now(timezone.utc))
            logger.warning("[Step 2/4] %s failed (non-critical): %s", step, e)

        # ── Step 3/4: Pool – update expiry time ──────────────────────────
        step = "pool.update_expires_at"
        logger.info("[Step 3/4] %s expires_at=%s", step, expires_at)
        update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
        try:
            if not expires_at:
                update_order_step(db, order_id, step, "skipped",
                                  log_output="Skipped: no requested_until",
                                  finished_at=datetime.now(timezone.utc))
            elif order.get("assigned_asset_id"):
                result = pool_manager.set_asset_busy(
                    db, order["assigned_asset_id"], order_id, expires_at
                )
                if not result["success"]:
                    raise RuntimeError(result["error"])
                update_order_step(db, order_id, step, "success",
                                  log_output=f"expires_at set to {expires_at}",
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
                by="celery:vdi_modify", ctx=str(self.request.id),
            )
            db.commit()
            logger.error("[Step 3/4] FAILED: %s", e)
            raise self.retry(exc=e)

        # ── Step 4/4: Notification ─────────────────────────────────────
        step = "notifications.send_modify_confirmation"
        logger.info("[Step 4/4] %s to %s", step, order["user_email"])
        update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
        try:
            result = notifications.send_provision_confirmation(
                user_email=order["user_email"],
                user_name=order["user_name"],
                asset_name=asset_name,
                rdp_users=order["rdp_users"] or [],
                expires_at=expires_at,
            )
            update_order_step(db, order_id, step, "success" if result["success"] else "failed",
                              log_output=str(result),
                              finished_at=datetime.now(timezone.utc))
        except Exception as e:
            logger.warning("[Step 4/4] Notification failed (non-critical): %s", e)
            update_order_step(db, order_id, step, "failed", error=str(e),
                              finished_at=datetime.now(timezone.utc))

        # ── Set order to DELIVERED ─────────────────────────────────────────
        update_order_status(db, order_id, "delivered")
        audit_helper.waudit(
            db, "order", order_id, "status_changed",
            old={"status": "processing"}, new={"status": "delivered"},
            by="celery:vdi_modify", ctx=str(self.request.id),
        )
        db.commit()

        logger.info("=== vdi_modify COMPLETE: order_id=%s ===", order_id)
        return {"success": True, "order_id": order_id}

    except Exception as e:
        logger.error("=== vdi_modify FAILED: order_id=%s error=%s ===", order_id, e)
        db.execute(
            text("UPDATE orders SET status = 'failed', error_message = :err, updated_at = NOW() WHERE id = :id"),
            {"err": str(e), "id": order_id},
        )
        audit_helper.waudit(
            db, "order", order_id, "status_changed",
            old={"status": "processing"}, new={"status": "failed", "error": str(e)},
            by="celery:vdi_modify",
        )
        db.commit()
        raise self.retry(exc=e)
    finally:
        db.close()
