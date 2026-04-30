"""Beat task that auto-declines pending approvals past their inactivity window.

Off by default. Activates when the operator sets both
``approval.auto_decline_enabled = true`` and
``approval.auto_decline_after_days > 0``.

Mirrors the decline path in ``app.utils.approval_decision`` (api side):

* Approval row → ``status='declined'``, ``decided_at=NOW()``, ``comment``
  set to the configured message (audit-traceable as a system action).
* Order → ``status='rejected'``, ``error_message`` populated.
* Two audit rows (``order_approval`` + ``order``) using the
  shared ``waudit`` helper.
* Rejection email dispatched via the existing
  ``tasks.workflows.dynamic_runner.send_approval_result_email``
  task so the requester gets the same message they'd get from a
  human-driven decline.

Multi-approver orders: a single decline is a hard veto everywhere
else in the codebase, so we follow the same rule. Other pending
approvals on the same order stay pending — they're harmless once
the order is rejected, and any subsequent decision on them is a
no-op against the already-final order.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from celery import Celery
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tasks import app
from tasks.modules.audit_helper import classify_asset_type_config, waudit
from tasks.modules.config_reader import get_config

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")
BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")


def _get_db_session() -> Session:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return Session(engine)


def _truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in ("true", "1", "yes", "on", "enabled")


@app.task(name="tasks.workflows.approval_auto_decline.scan_and_auto_decline")
def scan_and_auto_decline() -> dict:
    """Decline pending approvals older than the configured threshold."""
    db = _get_db_session()
    try:
        if not _truthy(get_config(db, "approval.auto_decline_enabled", "false")):
            return {"success": True, "skipped": True, "reason": "auto_decline_enabled is false"}

        try:
            after_days = int(get_config(db, "approval.auto_decline_after_days", "0") or "0")
        except (TypeError, ValueError):
            after_days = 0
        if after_days <= 0:
            return {"success": True, "skipped": True, "reason": "auto_decline_after_days <= 0"}

        decline_msg = (
            get_config(db, "approval.auto_decline_message", "")
            or "Auto-declined after the configured inactivity window."
        ).strip()

        cutoff = datetime.now(timezone.utc) - timedelta(days=after_days)

        # Pull at most one stale pending approval per order — a single
        # decline already vetoes the order (matches apply_approval_decision
        # semantics), so handling the rest in the same tick would just
        # write redundant audit rows. The DISTINCT ON picks the oldest
        # pending row per order so the audit trail names the right
        # approver as the "victim" of the auto-decline.
        rows = db.execute(
            text("""
                SELECT DISTINCT ON (oa.order_id)
                  oa.id              AS approval_id,
                  oa.order_id        AS order_id,
                  oa.approver_email,
                  oa.approver_name,
                  oa.approver_type,
                  oa.rule_name,
                  o.status::text     AS order_status,
                  at.id              AS asset_type_id,
                  at.config          AS asset_type_config
                FROM order_approvals oa
                JOIN orders      o  ON o.id  = oa.order_id
                JOIN asset_types at ON at.id = o.asset_type_id
                WHERE oa.status = 'pending'
                  AND oa.created_at < :cutoff
                  AND o.status::text NOT IN ('rejected', 'cancelled')
                ORDER BY oa.order_id, oa.created_at ASC
            """),
            {"cutoff": cutoff},
        ).fetchall()

        if not rows:
            return {"success": True, "declined": 0, "after_days": after_days}

        celery_app = Celery(broker=BROKER_URL)
        declined_count = 0

        for r in rows:
            now = datetime.now(timezone.utc)
            classification = classify_asset_type_config(r.asset_type_config)
            actor = "system:auto_decline"
            error_message = (
                f"Auto-declined (no decision in {after_days} day"
                f"{'s' if after_days != 1 else ''}): {decline_msg}"
            )

            # Decline this approval row.
            db.execute(
                text("""
                    UPDATE order_approvals
                    SET status     = 'declined',
                        decided_at = :now,
                        comment    = :comment
                    WHERE id = :id
                      AND status = 'pending'
                """),
                {"now": now, "comment": decline_msg, "id": r.approval_id},
            )

            # Reject the order if not already in a terminal state.
            db.execute(
                text("""
                    UPDATE orders
                    SET status        = 'rejected',
                        error_message = :err
                    WHERE id = :id
                      AND status::text NOT IN ('rejected', 'cancelled')
                """),
                {"err": error_message, "id": r.order_id},
            )

            # Audit: per-approval decline + order status transition.
            waudit(
                db, "order_approval", r.approval_id, "declined",
                new={
                    "approver_email": r.approver_email,
                    "approver_type":  r.approver_type,
                    "rule_name":      r.rule_name,
                    "comment":        decline_msg,
                    "auto":           True,
                    "after_days":     after_days,
                },
                by=actor, classification=classification,
            )
            waudit(
                db, "order", r.order_id, "status_changed",
                old={"status": r.order_status},
                new={"status": "rejected", "reason": error_message},
                by=actor, classification=classification,
            )

            # Notify the requester via the same email path human declines use.
            celery_app.send_task(
                "tasks.workflows.dynamic_runner.send_approval_result_email",
                args=[r.order_id, False, r.approver_name or "ip·Solis", decline_msg],
                queue="provision",
            )

            declined_count += 1
            logger.info(
                "Auto-declined approval %s on order %s (approver=%s, after_days=%d)",
                r.approval_id, r.order_id, r.approver_email, after_days,
            )

        db.commit()

        return {
            "success": True,
            "declined": declined_count,
            "after_days": after_days,
        }
    finally:
        db.close()
