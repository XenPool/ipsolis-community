"""Runbook: VDI Provision – Vollständige VDI-Bereitstellung.

Entspricht dem Ivanti-Runbook 'VDI Bereitstellen'.

Ablauf (sequenziell):
  1. Pool: Freie VM reservieren
  2. Active Roles: RDP-Gruppe befüllen
  3. Active Roles: Admin-Gruppe befüllen
  4. vSphere: VMware Tools aktualisieren
  5. vSphere: VM rebooten
  6. Notification: Bereitstellungsmail senden
  7. Pool: Status auf BUSY setzen, Ablaufzeit eintragen
"""

import logging
import os
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tasks import app
from tasks.modules import active_roles, notifications, pool_manager, vsphere

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://xpuser:changeme@localhost:5432/itselfservice",
).replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _get_db_session() -> Session:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return Session(engine)


def _get_order(db: Session, order_id: int) -> dict:
    """Lädt Order-Daten aus DB (minimale Implementierung für Grundgerüst)."""
    from sqlalchemy import text
    row = db.execute(
        text("""
            SELECT o.id, o.user_email, o.user_name, o.asset_type_id,
                   o.rdp_users, o.admin_users, o.requested_until, o.status
            FROM orders o WHERE o.id = :order_id
        """),
        {"order_id": order_id},
    ).fetchone()
    if not row:
        raise ValueError(f"Order {order_id} not found")
    return row._asdict()


def _update_order_step(
    db: Session,
    order_id: int,
    step_name: str,
    status: str,
    log_output: str | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    from sqlalchemy import text
    db.execute(
        text("""
            INSERT INTO order_steps (order_id, step_name, status, started_at, finished_at, log_output, error)
            VALUES (:order_id, :step_name, :status, :started_at, :finished_at, :log_output, :error)
        """),
        {
            "order_id": order_id,
            "step_name": step_name,
            "status": status,
            "started_at": started_at or datetime.now(timezone.utc),
            "finished_at": finished_at,
            "log_output": log_output,
            "error": error,
        },
    )
    db.commit()


def _update_order_status(db: Session, order_id: int, status: str, error: str | None = None) -> None:
    from sqlalchemy import text
    db.execute(
        text("UPDATE orders SET status = :status, error_message = :error, updated_at = NOW() WHERE id = :id"),
        {"status": status, "error": error, "id": order_id},
    )
    db.commit()


@app.task(
    name="tasks.workflows.vdi_provision.run",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue="provision",
)
def run(self: Task, order_id: int) -> dict:
    """
    Hauptrunbook: VDI bereitstellen.

    Args:
        order_id: ID der Order in der Datenbank

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

    # ── Schritt 1: VM aus Pool reservieren ────────────────────────────────────
    step = "pool.reserve_asset"
    logger.info("[Step 1/7] %s", step)
    _update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = pool_manager.reserve_asset(db, order_id, order["asset_type_id"], expires_at)
        if not result["success"]:
            raise RuntimeError(result["error"])
        asset_id = result["asset_id"]
        asset_name = result["asset_name"]
        _update_order_step(
            db, order_id, step, "success",
            log_output=f"Reserved: {asset_name} (id={asset_id})",
            finished_at=datetime.now(timezone.utc),
        )
        # assigned_asset_id in Order eintragen
        from sqlalchemy import text
        db.execute(
            text("UPDATE orders SET assigned_asset_id = :aid WHERE id = :oid"),
            {"aid": asset_id, "oid": order_id},
        )
        db.commit()
    except Exception as e:
        _update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        _update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 1/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Schritt 2: Active Roles – RDP-Gruppe ──────────────────────────────────
    step = "active_roles.set_rdp_group"
    logger.info("[Step 2/7] %s – users: %s", step, order["rdp_users"])
    _update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = active_roles.set_rdp_group(asset_name, order["rdp_users"] or [])
        if not result["success"]:
            raise RuntimeError(result["error"])
        _update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        _update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        _update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 2/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Schritt 3: Active Roles – Admin-Gruppe ────────────────────────────────
    step = "active_roles.set_admin_group"
    logger.info("[Step 3/7] %s – users: %s", step, order["admin_users"])
    _update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = active_roles.set_admin_group(asset_name, order["admin_users"] or [])
        if not result["success"]:
            raise RuntimeError(result["error"])
        _update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        _update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        _update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 3/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Schritt 4: VMware Tools aktualisieren ─────────────────────────────────
    step = "vsphere.update_vmware_tools"
    logger.info("[Step 4/7] %s on '%s'", step, asset_name)
    _update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = vsphere.update_vmware_tools(asset_name)
        if not result["success"]:
            raise RuntimeError(result["error"])
        _update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        _update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        _update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 4/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Schritt 5: VM rebooten ────────────────────────────────────────────────
    step = "vsphere.restart_vm"
    logger.info("[Step 5/7] %s on '%s'", step, asset_name)
    _update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = vsphere.restart_vm(asset_name)
        if not result["success"]:
            raise RuntimeError(result["error"])
        _update_order_step(
            db, order_id, step, "success",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        _update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        _update_order_status(db, order_id, "failed", str(e))
        logger.error("[Step 5/7] FAILED: %s", e)
        raise self.retry(exc=e)

    # ── Schritt 6: Bereitstellungsmail senden ─────────────────────────────────
    step = "notifications.send_provision_confirmation"
    logger.info("[Step 6/7] %s to %s", step, order["user_email"])
    _update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = notifications.send_provision_confirmation(
            user_email=order["user_email"],
            user_name=order["user_name"],
            asset_name=asset_name,
            rdp_users=order["rdp_users"] or [],
            expires_at=expires_at,
        )
        _update_order_step(
            db, order_id, step, "success" if result["success"] else "failed",
            log_output=str(result),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        # Notification-Fehler sind nicht kritisch – weiter mit Schritt 7
        logger.warning("[Step 6/7] Notification failed (non-critical): %s", e)
        _update_order_step(
            db, order_id, step, "failed",
            error=str(e),
            finished_at=datetime.now(timezone.utc),
        )

    # ── Schritt 7: Asset auf BUSY setzen ──────────────────────────────────────
    step = "pool.set_asset_busy"
    logger.info("[Step 7/7] %s", step)
    _update_order_step(db, order_id, step, "running", started_at=datetime.now(timezone.utc))
    try:
        result = pool_manager.set_asset_busy(db, asset_id, order_id, expires_at)
        if not result["success"]:
            raise RuntimeError(result["error"])
        _update_order_step(
            db, order_id, step, "success",
            log_output=f"Asset {asset_name} is now BUSY until {expires_at}",
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        _update_order_step(db, order_id, step, "failed", error=str(e), finished_at=datetime.now(timezone.utc))
        logger.error("[Step 7/7] FAILED: %s", e)

    # ── Order auf DELIVERED setzen ────────────────────────────────────────────
    _update_order_status(db, order_id, "delivered")
    logger.info(
        "=== vdi_provision COMPLETE: order_id=%s asset=%s ===",
        order_id, asset_name,
    )

    return {"success": True, "order_id": order_id, "asset_name": asset_name, "asset_id": asset_id}
