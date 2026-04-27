"""Beat task that nudges stale pending approvals.

Runs hourly. For every approval row in ``status='pending'`` whose
``created_at`` is older than ``approval.reminder_after_hours`` and that
has not been reminded in the same window, re-send the email and
(if configured) the Teams card. Stops after ``approval.max_reminders``
attempts to avoid spamming approvers.

Reuses ``dynamic_runner.deliver_approval_notification`` so the reminder
delivery path is identical to the initial dispatch — same template,
same adaptive card builder, same signed approval token.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tasks import app
from tasks.modules.config_reader import get_config

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _get_db_session() -> Session:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return Session(engine)


def _truthy(s: str | None) -> bool:
    return (s or "").strip().lower() in ("true", "1", "yes", "on", "enabled")


@app.task(name="tasks.workflows.approval_reminders.scan_and_remind")
def scan_and_remind() -> dict:
    """Scan stale pending approvals: nudge new ones, escalate exhausted ones."""
    db = _get_db_session()
    try:
        if not _truthy(get_config(db, "approval.reminders_enabled", "true")):
            return {"success": True, "skipped": True, "reason": "approval.reminders_enabled is false"}

        try:
            after_hours = max(1, int(get_config(db, "approval.reminder_after_hours", "24") or "24"))
        except (TypeError, ValueError):
            after_hours = 24
        try:
            max_reminders = max(0, int(get_config(db, "approval.max_reminders", "3") or "3"))
        except (TypeError, ValueError):
            max_reminders = 3

        cutoff = datetime.now(timezone.utc) - timedelta(hours=after_hours)
        portal_base = get_config(db, "portal.base_url", "http://localhost:8000")
        teams_mode = (get_config(db, "teams.mode", "disabled") or "disabled").strip()
        teams_webhook = (get_config(db, "teams.webhook_url") or "").strip()
        app_title = get_config(db, "app.title", "ip·Solis") or "ip·Solis"
        escalation_emails_raw = (get_config(db, "approval.escalation_email") or "").strip()
        escalation_emails = [a.strip() for a in escalation_emails_raw.split(",") if a.strip()]

        # ── Reminders: row not yet at cap, last touch older than cutoff ─────
        rows = db.execute(
            text("""
                SELECT
                  oa.id           AS approval_id,
                  oa.approver_email, oa.approver_name,
                  oa.reminder_count,
                  o.user_email, o.user_name,
                  o.requested_from, o.requested_until,
                  at.name AS asset_type_name
                FROM order_approvals oa
                JOIN orders      o  ON o.id  = oa.order_id
                JOIN asset_types at ON at.id = o.asset_type_id
                WHERE oa.status = 'pending'
                  AND oa.escalated_at IS NULL
                  AND oa.reminder_count < :max_reminders
                  AND COALESCE(oa.last_reminded_at, oa.created_at) < :cutoff
                ORDER BY oa.created_at ASC
            """),
            {"max_reminders": max_reminders, "cutoff": cutoff},
        ).fetchall()

        from tasks.workflows.dynamic_runner import deliver_approval_notification

        reminded = 0
        teams_sent = 0
        for r in rows:
            from_date = r.requested_from.strftime("%d.%m.%Y") if r.requested_from else ""
            until_date = r.requested_until.strftime("%d.%m.%Y") if r.requested_until else ""

            email_ok, teams_ok = deliver_approval_notification(
                db,
                approval_id=r.approval_id,
                approver_email=r.approver_email,
                approver_name=r.approver_name,
                requester_name=r.user_name or "",
                requester_email=r.user_email or "",
                asset_type_name=r.asset_type_name or "",
                from_date=from_date,
                until_date=until_date,
                portal_base=portal_base,
                teams_mode=teams_mode,
                teams_webhook=teams_webhook,
                app_title=app_title,
                is_reminder=True,
                reminder_count=(r.reminder_count or 0) + 1,
            )
            if email_ok:
                reminded += 1
            if teams_ok:
                teams_sent += 1

            db.execute(
                text("""
                    UPDATE order_approvals
                    SET reminder_count = reminder_count + 1,
                        last_reminded_at = NOW()
                    WHERE id = :id
                """),
                {"id": r.approval_id},
            )

        # ── Escalations: row at cap, never escalated, escalation configured ─
        escalated = 0
        if escalation_emails and max_reminders > 0:
            esc_rows = db.execute(
                text("""
                    SELECT
                      oa.id           AS approval_id,
                      oa.approver_email, oa.approver_name,
                      oa.reminder_count,
                      o.user_email, o.user_name,
                      o.requested_from, o.requested_until,
                      at.name AS asset_type_name
                    FROM order_approvals oa
                    JOIN orders      o  ON o.id  = oa.order_id
                    JOIN asset_types at ON at.id = o.asset_type_id
                    WHERE oa.status = 'pending'
                      AND oa.reminder_count >= :max_reminders
                      AND oa.escalated_at IS NULL
                """),
                {"max_reminders": max_reminders},
            ).fetchall()

            from tasks.modules import notifications as notif

            for r in esc_rows:
                from_date = r.requested_from.strftime("%d.%m.%Y") if r.requested_from else ""
                until_date = r.requested_until.strftime("%d.%m.%Y") if r.requested_until else ""

                # Approval URL points the escalation contact at the order in
                # the admin UI, not a signed-token page (they don't decide,
                # they intervene operationally).
                approval_url = f"{portal_base.rstrip('/')}/ui/orders"

                try:
                    notif.send_approval_escalated(
                        db,
                        escalation_emails=escalation_emails,
                        approver_email=r.approver_email,
                        approver_name=r.approver_name,
                        requester_name=r.user_name or "",
                        requester_email=r.user_email or "",
                        asset_type_name=r.asset_type_name or "",
                        reminder_count=r.reminder_count or 0,
                        from_date=from_date,
                        until_date=until_date,
                        approval_url=approval_url,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Escalation send failed for approval %s: %s", r.approval_id, exc)
                    continue

                db.execute(
                    text("UPDATE order_approvals SET escalated_at = NOW() WHERE id = :id"),
                    {"id": r.approval_id},
                )
                escalated += 1
                logger.info(
                    "Approval %s escalated (original approver=%s, reminders=%d) to %s",
                    r.approval_id, r.approver_email, r.reminder_count or 0,
                    ", ".join(escalation_emails),
                )

        db.commit()

        if not rows and escalated == 0:
            return {"success": True, "reminded": 0, "escalated": 0}

        logger.info(
            "Approval scan: %d reminders, %d teams cards, %d escalations (cutoff=%dh, cap=%d).",
            reminded, teams_sent, escalated, after_hours, max_reminders,
        )
        return {
            "success": True,
            "reminded": reminded,
            "teams_sent": teams_sent,
            "escalated": escalated,
            "after_hours": after_hours,
            "max_reminders": max_reminders,
        }
    finally:
        db.close()
