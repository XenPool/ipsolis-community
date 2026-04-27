"""Celery Beat task: daily license expiry check.

Runs once per day (see ``beat_schedule`` in ``worker/tasks/__init__.py``).
Loads the current license and logs a warning when it is within 30/14/7 days
of expiry, or an error if already expired. When ``health.alert_enabled`` is
on and ``health.alert_email`` is configured, also sends an alert email.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy import text

from tasks.modules.maintenance import _db
from tasks.utils.license import load_license

logger = logging.getLogger(__name__)

_WARN_THRESHOLDS_DAYS = (30, 14, 7)


@shared_task(name="tasks.workflows.license_check.check_license_expiry", bind=True)
def check_license_expiry(self) -> dict:
    """Daily check. Returns a summary dict; logs/emails side-effects."""
    info = load_license(force_reload=True)

    if info.expires_at is None:
        # Community install or perpetual license — nothing to do.
        return {"status": "skipped", "reason": "no-expiry"}

    now = datetime.now(timezone.utc)
    days_remaining = (info.expires_at - now).days

    if days_remaining < 0:
        level_msg = f"License EXPIRED {abs(days_remaining)} day(s) ago ({info.expires_at.date().isoformat()})"
        logger.error(level_msg)
        _maybe_send_alert(
            subject="[ip·Solis] License expired",
            html_body=(
                f"<p>The ip·Solis Enterprise license has <strong>expired</strong>.</p>"
                f"<p>Licensee: {info.licensee}<br>"
                f"Expired on: {info.expires_at.date().isoformat()}</p>"
                f"<p>The instance has fallen back to Community edition. "
                f"Contact info@xenpool.com to renew.</p>"
            ),
        )
        return {"status": "expired", "days": days_remaining, "licensee": info.licensee}

    # Only warn once per threshold crossing — log at the most specific threshold.
    matched = next((d for d in _WARN_THRESHOLDS_DAYS if days_remaining <= d), None)
    if matched is None:
        return {"status": "ok", "days": days_remaining}

    msg = (
        f"ip·Solis license expires in {days_remaining} day(s) on "
        f"{info.expires_at.date().isoformat()} (licensee: {info.licensee})"
    )
    logger.warning(msg)
    _maybe_send_alert(
        subject=f"[ip·Solis] License expires in {days_remaining} days",
        html_body=(
            f"<p>The ip·Solis Enterprise license will expire in "
            f"<strong>{days_remaining} day(s)</strong>.</p>"
            f"<p>Licensee: {info.licensee}<br>"
            f"Expires on: {info.expires_at.date().isoformat()}</p>"
            f"<p>Contact info@xenpool.com to renew before expiry.</p>"
        ),
    )
    return {"status": "warning", "days": days_remaining, "threshold": matched}


def _maybe_send_alert(subject: str, html_body: str) -> None:
    """Send an email alert if health-alert email is configured. Never raises."""
    try:
        db = _db()
        try:
            enabled_row = db.execute(
                text("SELECT value FROM app_config WHERE key = 'health.alert_enabled'")
            ).first()
            if not enabled_row or (enabled_row[0] or "").strip().lower() not in ("true", "1", "yes"):
                return

            email_row = db.execute(
                text("SELECT value FROM app_config WHERE key = 'health.alert_email'")
            ).first()
            to_addr = (email_row[0] if email_row else "") or ""
            if not to_addr.strip():
                return

            from tasks.modules.maintenance import _send_health_email
            _send_health_email(db, to_addr.strip(), subject, html_body)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — alert delivery must never break the task
        logger.warning("license-expiry alert email failed: %s", exc)
