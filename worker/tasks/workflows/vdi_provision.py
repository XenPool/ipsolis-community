"""Runbook: VDI Provision – Complete VDI provisioning.

Corresponds to the Ivanti runbook 'VDI Bereitstellen'.

Sequence (sequential):
  1. Pool: Reserve free VM
  2. Active Roles: populate RDP group
  3. Active Roles: populate admin group
  4. vSphere: update VMware Tools
  5. vSphere: reboot VM
  6. Notification: send provisioning email
  7. Pool: set status to BUSY, record expiry time
"""

import logging
import os
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tasks import app
from tasks.modules import active_directory, active_roles, audit_helper, notifications, pool_manager, vsphere
from tasks.modules.step_helper import update_order_step, update_order_status

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://xpuser:changeme@localhost:5432/itselfservice",
).replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _get_db_session() -> Session:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return Session(engine)


def _get_order(db: Session, order_id: int) -> dict:
    """Loads order data from DB."""
    from sqlalchemy import text
    row = db.execute(
        text("""
            SELECT o.id, o.user_email, o.user_name, o.owner_email, o.owner_name,
                   o.asset_type_id, o.rdp_users, o.admin_users,
                   o.requested_from, o.requested_until, o.status,
                   o.servicenow_ref, o.snow_req
            FROM orders o WHERE o.id = :order_id
        """),
        {"order_id": order_id},
    ).fetchone()
    if not row:
        raise ValueError(f"Order {order_id} not found")
    return row._asdict()


def _get_asset_type_info(db: Session, asset_type_id: int) -> dict:
    """Reads asset type name and description from DB."""
    from sqlalchemy import text
    row = db.execute(
        text("SELECT name, description FROM asset_types WHERE id = :id"),
        {"id": asset_type_id},
    ).fetchone()
    if not row:
        return {"name": f"Asset Type {asset_type_id}", "description": ""}
    return {"name": row[0], "description": row[1] or ""}


@app.task(
    name="tasks.workflows.vdi_provision.run",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue="provision",
)
def run(self: Task, order_id: int) -> dict:
    """
    Main runbook: provision VDI.

    Args:
        order_id: ID of the order in the database

    Returns:
        {"success": True, "asset_name": str, "order_id": int}
    """
    logger.info("=== vdi_provision START: order_id=%s ===", order_id)
    db = _get_db_session()

    try:
        order = _get_order(db, order_id)
    except ValueError as e:
        logger.error("Order not found: %s", e)
        return {"success": False, "error": str(e)}

    expires_at = order["requested_until"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)

    asset_name = None
    asset_id = None

    # ── Step 0: Send order confirmation (non-critical) ────────────────────────
    step = "order.confirmation"
    logger.info("[Step 0/7] %s", step)
    update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        asset_type_info = _get_asset_type_info(db, order["asset_type_id"])

        # Complete owner data via AD if needed
        owner_email = order.get("owner_email")
        owner_name = order.get("owner_name")
        if not owner_name and owner_email:
            ad_result = active_directory.lookup_user(owner_email, db)
            if ad_result["success"]:
                owner_name = ad_result["display_name"]
                owner_email = ad_result["email"]

        requested_from = order["requested_from"]
        if isinstance(requested_from, str):
            requested_from = datetime.fromisoformat(requested_from)

        result = notifications.send_order_confirmation(
            db=db,
            user_email=order["user_email"],
            user_name=order["user_name"],
            owner_email=owner_email,
            owner_name=owner_name,
            asset_type_name=asset_type_info["name"],
            asset_type_description=asset_type_info["description"],
            requested_from=requested_from,
            requested_until=expires_at,
            snow_req=order.get("snow_req"),
            snow_ritm=order.get("servicenow_ref"),
        )
        update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        # Confirmation email is not critical – continue runbook
        logger.warning("[Step 0/7] order.confirmation failed (non-critical): %s", e)
        update_order_step(
            db, order_id, step, "failed",
            error=str(e),
            finished_at=datetime.now(timezone.utc),
        )

    # ── Step 1: Reserve VM from pool ──────────────────────────────────────────
    step = "pool.reserve_asset"
    logger.info("[Step 1/7] %s", step)
    update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = pool_manager.reserve_asset(db, order_id, order["asset_type_id"], expires_at)
        if not result["success"]:
            raise RuntimeError(result["error"])
        asset_id = result["asset_id"]
        asset_name = result["asset_name"]
        update_order_step(
            db, order_id, step, "success",
            log_output=f"Reserved: {asset_name} (id={asset_id})",
            finished_at=datetime.now(timezone.utc),
        )
        # Write assigned_asset_id to order (only if asset exists in DB)
        from sqlalchemy import text
        import os as _os
        if _os.getenv("ENVIRONMENT", "development") != "development":
            db.execute(
                text("UPDATE orders SET assigned_asset_id = :aid WHERE id = :oid"),
                {"aid": asset_id, "oid": order_id},
            )
            db.commit()
    except Exception as e:
        update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        update_order_status(db, order_id, "failed", str(e))
        audit_helper.waudit(
            db, "order", order_id, "status_changed",
            old={"status": "processing"}, new={"status": "failed", "error": str(e)},
            by="celery:vdi_provision", ctx=str(self.request.id),
        )
        db.commit()
        logger.error("[Step 1/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Step 2: Active Roles – RDP group ──────────────────────────────────────
    step = "active_roles.set_rdp_group"
    logger.info("[Step 2/7] %s – users: %s", step, order["rdp_users"])
    update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = active_roles.set_rdp_group(asset_name, order["rdp_users"] or [])
        if not result["success"]:
            raise RuntimeError(result["error"])
        update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 2/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Step 3: Active Roles – Admin group ────────────────────────────────────
    step = "active_roles.set_admin_group"
    logger.info("[Step 3/7] %s – users: %s", step, order["admin_users"])
    update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = active_roles.set_admin_group(asset_name, order["admin_users"] or [])
        if not result["success"]:
            raise RuntimeError(result["error"])
        update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 3/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Step 4: Update VMware Tools ───────────────────────────────────────────
    step = "vsphere.update_vmware_tools"
    logger.info("[Step 4/7] %s on '%s'", step, asset_name)
    update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = vsphere.update_vmware_tools(asset_name)
        if not result["success"]:
            raise RuntimeError(result["error"])
        update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 4/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Step 5: Reboot VM ─────────────────────────────────────────────────────
    step = "vsphere.restart_vm"
    logger.info("[Step 5/7] %s on '%s'", step, asset_name)
    update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = vsphere.restart_vm(asset_name)
        if not result["success"]:
            raise RuntimeError(result["error"])
        update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 5/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Step 6: Send provisioning email ───────────────────────────────────────
    step = "notifications.send_provision_confirmation"
    logger.info("[Step 6/7] %s to %s", step, order["user_email"])
    update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = notifications.send_provision_confirmation(
            user_email=order["user_email"],
            user_name=order["user_name"],
            asset_name=asset_name,
            rdp_users=order["rdp_users"] or [],
            expires_at=expires_at,
        )
        update_order_step(
            db, order_id, step, "success" if result["success"] else "failed",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        # Notification errors are not critical – continue with step 7
        logger.warning("[Step 6/7] Notification failed (non-critical): %s", e)
        update_order_step(
            db, order_id, step, "failed",
            error=str(e),
            finished_at=datetime.now(timezone.utc),
        )

    # ── Step 7: Set asset to BUSY ─────────────────────────────────────────────
    step = "pool.set_asset_busy"
    logger.info("[Step 7/7] %s", step)
    update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = pool_manager.set_asset_busy(db, asset_id, order_id, expires_at)
        if not result["success"]:
            raise RuntimeError(result["error"])
        update_order_step(
            db, order_id, step, "success",
            log_output=f"Asset {asset_name} is now BUSY until {expires_at}",
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        logger.error("[Step 7/7] FAILED: %s", e)

    # ── Set order to DELIVERED ────────────────────────────────────────────────
    update_order_status(db, order_id, "delivered")
    audit_helper.waudit(
        db, "order", order_id, "status_changed",
        old={"status": "processing"}, new={"status": "delivered"},
        by="celery:vdi_provision", ctx=str(self.request.id),
    )
    db.commit()
    logger.info(
        "=== vdi_provision COMPLETE: order_id=%s asset=%s ===",
        order_id, asset_name,
    )

    return {"success": True, "order_id": order_id, "asset_name": asset_name, "asset_id": asset_id}
