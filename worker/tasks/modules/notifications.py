"""Module: Notifications – Email notifications.

Sends HTML emails via SMTP. All settings (SMTP, from-address, from-name)
are read from the app_config table. Email subject and body are rendered
from the email_templates table, allowing admins to customise content.
"""

import logging
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Env-var fallbacks (used if app_config has no value)
MAIL_FROM = os.getenv("MAIL_FROM", "noreply@example.com")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "XenPool IT Selfservice")
REMINDER_HOURS = int(os.getenv("REMINDER_HOURS_BEFORE_EXPIRY", "24"))

BRAND_COLOR = "#BB0A30"


# ── Template rendering ─────────────────────────────────────────────────────────

def _render_str(template: str, variables: dict) -> str:
    """Replaces {{variable_name}} placeholders with values from variables dict.
    Missing variables are replaced with an empty string.
    """
    def replace(match: re.Match) -> str:
        key = match.group(1).strip()
        val = variables.get(key)
        return str(val) if val is not None else ""
    return re.sub(r"\{\{([^}]+)\}\}", replace, template)


def _render_template(
    db: "Session",
    event_key: str,
    variables: dict,
) -> "tuple[str, str] | tuple[None, None]":
    """Loads template from DB and renders subject + body.

    Returns (rendered_subject, rendered_body_html) or (None, None) if the
    template does not exist or is_active=False.
    """
    from sqlalchemy import text as sql_text
    row = db.execute(
        sql_text(
            "SELECT subject, body FROM email_templates "
            "WHERE event_key = :key AND is_active = TRUE LIMIT 1"
        ),
        {"key": event_key},
    ).fetchone()
    if not row:
        logger.info("[notifications] No active template for event_key=%r – skipping email", event_key)
        return None, None
    subject = _render_str(row[0], variables)
    body = _render_str(row[1], variables)
    return subject, body


def _build_branded_html(body_content: str, company_name: str, subject_line: str) -> str:
    """Wraps admin-provided body HTML in the XenPool branded email wrapper."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:20px 0;">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:4px;overflow:hidden;">
      <tr>
        <td style="background:{BRAND_COLOR};padding:24px 32px;">
          <div style="color:#ffffff;font-size:22px;font-weight:bold;">{company_name} IT Self-Service</div>
          <div style="color:#ffffff;font-size:14px;margin-top:4px;opacity:0.9;">{subject_line}</div>
        </td>
      </tr>
      <tr>
        <td style="padding:28px 32px;line-height:1.6;color:#333333;font-size:14px;">
          {body_content}
        </td>
      </tr>
      <tr>
        <td style="background:#f8f8f8;padding:16px 32px;border-top:1px solid #eeeeee;">
          <p style="margin:0;font-size:11px;color:#aaa;text-align:center;">
            {company_name} IT Self-Service &nbsp;|&nbsp; This email was generated automatically.
          </p>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


# ── Public send functions ──────────────────────────────────────────────────────

def send_order_confirmation(
    db: "Session",
    user_email: str,
    user_name: str,
    owner_email: str | None,
    owner_name: str | None,
    asset_type_name: str,
    asset_type_description: str | None,
    requested_from: datetime,
    requested_until: datetime,
    snow_req: str | None,
    snow_ritm: str | None,
) -> dict:
    """Sends order confirmation to requester and optionally owner."""
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    effective_owner_email = owner_email or user_email
    effective_owner_name = owner_name or user_name

    recipients = [user_email]
    if owner_email and owner_email.lower() != user_email.lower():
        recipients.append(owner_email)

    variables = {
        "company_name": company_name,
        "requester_name": user_name,
        "requester_email": user_email,
        "owner_name": effective_owner_name,
        "owner_email": effective_owner_email,
        "asset_type_name": asset_type_name,
        "asset_type_description": asset_type_description or "",
        "from_date": requested_from.strftime("%d.%m.%Y"),
        "until_date": requested_until.strftime("%d.%m.%Y"),
        "snow_req": snow_req or "",
        "snow_ritm": snow_ritm or "",
    }

    subject, body = _render_template(db, "order_confirmation", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, company_name, subject)
    return _send_html_email_multi(db, recipients, bcc, mail_from, subject, html)


def send_provision_confirmation(
    db: "Session",
    user_email: str,
    user_name: str,
    asset_name: str,
    rdp_users: list[str],
    expires_at: datetime,
) -> dict:
    """Sends provisioning confirmation to the user."""
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "requester_name": user_name,
        "requester_email": user_email,
        "asset_name": asset_name,
        "rdp_users": ", ".join(rdp_users) if rdp_users else "(none)",
        "expires_at": expires_at.strftime("%d.%m.%Y %H:%M"),
    }

    subject, body = _render_template(db, "provision_confirmation", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, company_name, subject)
    return _send_html_email_multi(db, [user_email], bcc, mail_from, subject, html)


def send_expiry_reminder(
    db: "Session",
    user_email: str,
    user_name: str,
    asset_name: str,
    expires_at: datetime,
    hours_remaining: float,
) -> dict:
    """Sends expiry reminder email."""
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "requester_name": user_name,
        "requester_email": user_email,
        "asset_name": asset_name,
        "expires_at": expires_at.strftime("%d.%m.%Y %H:%M"),
        "hours_remaining": str(int(hours_remaining)),
    }

    subject, body = _render_template(db, "expiry_reminder", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, company_name, subject)
    return _send_html_email_multi(db, [user_email], bcc, mail_from, subject, html)


def send_reclaim_notification(
    db: "Session",
    user_email: str,
    user_name: str,
    asset_name: str,
) -> dict:
    """Notifies user about resource being returned to the pool."""
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "requester_name": user_name,
        "requester_email": user_email,
        "asset_name": asset_name,
    }

    subject, body = _render_template(db, "reclaim_notification", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, company_name, subject)
    return _send_html_email_multi(db, [user_email], bcc, mail_from, subject, html)


def send_expiry_reminders() -> dict:
    """Celery Beat task: sends reminder emails for expiring assets.
    Production DB query and dispatch to be implemented in a future sprint.
    """
    return {"sent": 0}


# ── SMTP helpers ───────────────────────────────────────────────────────────────

def _send_html_email_multi(
    db: "Session",
    recipients: list[str],
    bcc: str | None,
    mail_from: str,
    subject: str,
    html_body: str,
) -> dict:
    """Sends HTML email to multiple recipients (with optional BCC)."""
    return _production_send_html_email(db, recipients, bcc, mail_from, subject, html_body)


def _production_send_html_email(
    db: "Session",
    recipients: list[str],
    bcc: str | None,
    mail_from: str,
    subject: str,
    html_body: str,
) -> dict:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from tasks.modules.config_reader import get_config, get_config_int

    smtp_host = get_config(db, "email.smtp_server", "localhost")
    smtp_port = get_config_int(db, "email.smtp_port", 25)
    smtp_user = get_config(db, "email.username", "")
    smtp_password = get_config(db, "email.password", "")
    from_name = get_config(db, "email.from_name", MAIL_FROM_NAME)

    all_recipients = list(recipients)
    if bcc:
        all_recipients.append(bcc)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{mail_from}>"
    msg["To"] = ", ".join(recipients)
    if bcc:
        msg["Bcc"] = bcc
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_port == 587:
                server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.sendmail(mail_from, all_recipients, msg.as_string())
        logger.info("Email sent: to=%s subject=%r", recipients, subject)
        return {"success": True, "to": recipients}
    except Exception as e:
        logger.error("Email failed: to=%s error=%s", recipients, e)
        return {"success": False, "error": str(e)}
