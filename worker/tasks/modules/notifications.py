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
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "ip·Solis")
REMINDER_HOURS = int(os.getenv("REMINDER_HOURS_BEFORE_EXPIRY", "24"))

BRAND_COLOR = "#1e3a8a"


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


def _build_branded_html(body_content: str, app_title: str, subject_line: str) -> str:
    """Wraps admin-provided body HTML in the branded email wrapper."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:20px 0;">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:4px;overflow:hidden;">
      <tr>
        <td style="background:{BRAND_COLOR};padding:24px 32px;">
          <div style="color:#ffffff;font-size:22px;font-weight:bold;">{app_title}</div>
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
            {app_title} &nbsp;|&nbsp; This email was generated automatically.
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
    scheduled_date: str | None = None,
) -> dict:
    """Sends order confirmation to requester and optionally owner.

    If scheduled_date is set, the email template can include {{scheduled_note}}
    to inform the user that execution is delayed until that date.
    """
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    effective_owner_email = owner_email or user_email
    effective_owner_name = owner_name or user_name

    recipients = [user_email]
    if owner_email and owner_email.lower() != user_email.lower():
        recipients.append(owner_email)

    # Build scheduled note for email template
    scheduled_note = ""
    if scheduled_date:
        scheduled_note = f"Your order is scheduled and will be automatically executed on {scheduled_date}."

    variables = {
        "company_name": company_name,
        "app_title": app_title,
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
        "scheduled_date": scheduled_date or "",
        "scheduled_note": scheduled_note,
    }

    subject, body = _render_template(db, "order_confirmation", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, recipients, bcc, mail_from, subject, html)


def send_provision_confirmation(
    db: "Session",
    user_email: str,
    user_name: str,
    asset_name: str,
    rdp_users: list[str],
    expires_at: datetime,
    rdp_hostname: str | None = None,
    rds_gateway_url: str | None = None,
    asset_type_name: str | None = None,
) -> dict:
    """Sends provisioning confirmation to the user.

    If rdp_hostname is provided (personal VDI assignment), a .rdp file is
    attached so the user can connect directly by opening the attachment.
    If rds_gateway_url is provided, it is included in the email template
    variables so the user knows where to connect.
    """
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    # Build RDS gateway info block (HTML) for the template
    rds_gateway_info = ""
    if rds_gateway_url:
        rds_gateway_info = (
            f'<p>Connect via the RDS Gateway: '
            f'<a href="{rds_gateway_url}" style="color:#1e3a8a;font-weight:bold;">{rds_gateway_url}</a></p>'
        )

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "requester_name": user_name,
        "requester_email": user_email,
        "asset_name": asset_name or "",
        "asset_type_name": asset_type_name or "",
        "rdp_users": ", ".join(rdp_users) if rdp_users else "",
        "expires_at": expires_at.strftime("%d.%m.%Y %H:%M"),
        "rds_gateway_url": rds_gateway_url or "",
        "rds_gateway_info": rds_gateway_info,
    }

    subject, body = _render_template(db, "provision_confirmation", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(
        db, [user_email], bcc, mail_from, subject, html,
        rdp_hostname=rdp_hostname,
    )


def send_modify_confirmation(
    db: "Session",
    user_email: str,
    user_name: str,
    asset_name: str,
    rdp_users: list[str],
    expires_at: datetime,
    rdp_hostname: str | None = None,
) -> dict:
    """Sends access-change confirmation for modify orders (personal VDI only).

    If rdp_hostname is provided, a .rdp file is attached.
    Silently skipped if no active 'modify_confirmation' template exists.
    """
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "requester_name": user_name,
        "requester_email": user_email,
        "asset_name": asset_name,
        "rdp_users": ", ".join(rdp_users) if rdp_users else "(none)",
        "expires_at": expires_at.strftime("%d.%m.%Y %H:%M"),
    }

    subject, body = _render_template(db, "modify_confirmation", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(
        db, [user_email], bcc, mail_from, subject, html,
        rdp_hostname=rdp_hostname,
    )


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
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "requester_name": user_name,
        "requester_email": user_email,
        "asset_name": asset_name,
        "expires_at": expires_at.strftime("%d.%m.%Y %H:%M"),
        "hours_remaining": str(int(hours_remaining)),
    }

    subject, body = _render_template(db, "expiry_reminder", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, [user_email], bcc, mail_from, subject, html)


def send_reclaim_notification(
    db: "Session",
    user_email: str,
    user_name: str,
    asset_name: str,
    asset_type_name: str | None = None,
) -> dict:
    """Notifies user about resource being revoked / returned to the pool."""
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "requester_name": user_name,
        "requester_email": user_email,
        "asset_name": asset_name or "",
        "asset_type_name": asset_type_name or "",
    }

    subject, body = _render_template(db, "reclaim_notification", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, [user_email], bcc, mail_from, subject, html)


def send_approval_request(
    db: "Session",
    approver_email: str,
    approver_name: str,
    requester_name: str,
    requester_email: str,
    asset_type_name: str,
    from_date: str = "",
    until_date: str = "",
    approval_url: str = "",
) -> dict:
    """Sends an approval request email to a manager or application owner."""
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "approver_name": approver_name,
        "requester_name": requester_name,
        "requester_email": requester_email,
        "asset_type_name": asset_type_name,
        "from_date": from_date,
        "until_date": until_date,
        "approval_url": approval_url or "",
    }

    subject, body = _render_template(db, "approval_request", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, [approver_email], bcc, mail_from, subject, html)


def send_approval_escalated(
    db: "Session",
    *,
    escalation_emails: list[str],
    approver_email: str,
    approver_name: str,
    requester_name: str,
    requester_email: str,
    asset_type_name: str,
    reminder_count: int,
    from_date: str = "",
    until_date: str = "",
    approval_url: str = "",
) -> dict:
    """Notify the configured escalation contact(s) that an approval has burned
    through all its reminders without a decision.

    The escalation email is informational — it doesn't carry a signed
    approve/decline link. Recipients chase the original approver,
    reassign the request, or cancel via the admin UI.
    """
    from tasks.modules.config_reader import get_config

    addrs = [a.strip() for a in (escalation_emails or []) if a and a.strip()]
    if not addrs:
        return {"success": True, "skipped": True, "reason": "no escalation_email configured"}

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "approver_name": approver_name,
        "approver_email": approver_email,
        "requester_name": requester_name,
        "requester_email": requester_email,
        "asset_type_name": asset_type_name,
        "from_date": from_date,
        "until_date": until_date,
        "approval_url": approval_url or "",
        "reminder_count": reminder_count,
    }

    subject, body = _render_template(db, "approval_escalated", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, addrs, bcc, mail_from, subject, html)


def send_approval_escalation_assigned(
    db: "Session",
    *,
    recipient_email: str,
    recipient_name: str,
    approver_email: str,
    approver_name: str,
    requester_name: str,
    requester_email: str,
    asset_type_name: str,
    from_date: str,
    until_date: str,
    approval_url: str,
) -> dict:
    """Notify a single escalation contact that an approval has been
    reassigned to them.

    Unlike ``send_approval_escalated`` (notify-only, points at the
    admin UI), this email carries a tokenized one-click decide link
    bound to the contact's *new* OrderApproval row — created upstream
    in the escalation flow when ``approval.escalation_assign=true``.
    The recipient can approve / reject directly from the email.

    One row per recipient (caller iterates over the configured
    ``approval.escalation_email`` list); the escalation tokens are
    independent so the recipients can each respond on their own.
    """
    from tasks.modules.config_reader import get_config

    if not recipient_email or not recipient_email.strip():
        return {"success": True, "skipped": True, "reason": "empty recipient"}

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        # ``approver_*`` here means the *original* approver who let the
        # request lapse — same naming as the notify-only template so
        # admins can reuse copy with `s/approval_escalated/approval_escalation_assigned/`.
        "approver_name": approver_name,
        "approver_email": approver_email,
        "requester_name": requester_name,
        "requester_email": requester_email,
        "asset_type_name": asset_type_name,
        "from_date": from_date,
        "until_date": until_date,
        "approval_url": approval_url,
    }

    subject, body = _render_template(db, "approval_escalation_assigned", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, [recipient_email.strip()], bcc, mail_from, subject, html)


def send_cost_threshold_breach(
    db: "Session",
    *,
    recipients: list[str],
    cost_center: str,
    currency: str,
    monthly_limit: float,
    projected_total: float,
    active_orders: int,
    asset_types: int,
    quiet_hours: int,
    cost_report_url: str = "",
) -> dict:
    """Notify recipients that projected monthly spend on a (cost_center,
    currency) crossed the configured limit. Best-effort and additive — the
    Beat task records ``last_alerted_at`` regardless of email outcome so a
    flaky SMTP relay doesn't lock the alert into a re-fire loop.
    """
    from tasks.modules.config_reader import get_config

    addrs = [a.strip() for a in (recipients or []) if a and a.strip()]
    if not addrs:
        return {"success": True, "skipped": True, "reason": "no recipients"}

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "cost_center": cost_center,
        "currency": currency,
        "monthly_limit": f"{monthly_limit:.2f}",
        "projected_total": f"{projected_total:.2f}",
        "active_orders": active_orders,
        "asset_types": asset_types,
        "cost_report_url": cost_report_url or "",
        "quiet_hours": quiet_hours,
    }

    subject, body = _render_template(db, "cost_threshold_breach", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, addrs, bcc, mail_from, subject, html)


def send_certification_kickoff(
    db: "Session",
    *,
    reviewer_email: str,
    reviewer_name: str | None,
    campaign_name: str,
    campaign_id: int,
    review_count: int,
    due_date: str,
    review_url: str,
) -> dict:
    """Email the reviewer at campaign kickoff with a link to their queue."""
    from tasks.modules.config_reader import get_config

    if not reviewer_email or not reviewer_email.strip():
        return {"success": True, "skipped": True, "reason": "no reviewer_email"}

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "reviewer_name": reviewer_name or reviewer_email,
        "reviewer_email": reviewer_email,
        "campaign_name": campaign_name,
        "campaign_id": campaign_id,
        "review_count": review_count,
        "due_date": due_date,
        "review_url": review_url,
    }
    subject, body = _render_template(db, "certification_kickoff", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}
    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, [reviewer_email], bcc, mail_from, subject, html)


def send_certification_reminder(
    db: "Session",
    *,
    reviewer_email: str,
    reviewer_name: str | None,
    campaign_name: str,
    campaign_id: int,
    pending_count: int,
    days_left: int,
    due_date: str,
    review_url: str,
) -> dict:
    """Day-N-before-due reminder to a reviewer with pending decisions."""
    from tasks.modules.config_reader import get_config

    if not reviewer_email or not reviewer_email.strip():
        return {"success": True, "skipped": True, "reason": "no reviewer_email"}

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "reviewer_name": reviewer_name or reviewer_email,
        "reviewer_email": reviewer_email,
        "campaign_name": campaign_name,
        "campaign_id": campaign_id,
        "pending_count": pending_count,
        "days_left": days_left,
        "due_date": due_date,
        "review_url": review_url,
    }
    subject, body = _render_template(db, "certification_reminder", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}
    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, [reviewer_email], bcc, mail_from, subject, html)


def send_certification_overdue(
    db: "Session",
    *,
    reviewer_email: str,
    reviewer_name: str | None,
    campaign_name: str,
    campaign_id: int,
    pending_count: int,
    due_date: str,
    review_url: str,
    auto_revoke_enabled: bool,
) -> dict:
    """Past-due nag email to a reviewer with pending decisions."""
    from tasks.modules.config_reader import get_config

    if not reviewer_email or not reviewer_email.strip():
        return {"success": True, "skipped": True, "reason": "no reviewer_email"}

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    if auto_revoke_enabled:
        warn = (
            '<p style="color:#BB0A30;"><strong>Heads up:</strong> auto-revoke is '
            'enabled for this tenant — pending reviews will be revoked '
            'automatically by the daily Beat task. Decide now to keep the '
            'access; otherwise it will be pulled.</p>'
        )
    else:
        warn = (
            '<p>Auto-revoke is not enabled for this tenant. Pending reviews '
            'will not be acted on automatically — please decide soon to '
            'satisfy the audit cycle.</p>'
        )

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "reviewer_name": reviewer_name or reviewer_email,
        "reviewer_email": reviewer_email,
        "campaign_name": campaign_name,
        "campaign_id": campaign_id,
        "pending_count": pending_count,
        "due_date": due_date,
        "review_url": review_url,
        "auto_revoke_warning": warn,
    }
    subject, body = _render_template(db, "certification_overdue", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}
    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, [reviewer_email], bcc, mail_from, subject, html)


def send_certification_escalation(
    db: "Session",
    *,
    escalation_emails: list[str],
    campaign_name: str,
    campaign_id: int,
    due_date: str,
    pending_count: int,
    reviewer_count: int,
    reviewer_summary: str,
    campaign_url: str,
    auto_revoke_status: str,
) -> dict:
    """One-shot escalation to the configured contact list when a campaign goes overdue."""
    from tasks.modules.config_reader import get_config

    addrs = [a.strip() for a in (escalation_emails or []) if a and a.strip()]
    if not addrs:
        return {"success": True, "skipped": True, "reason": "no escalation_email configured"}

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "campaign_name": campaign_name,
        "campaign_id": campaign_id,
        "due_date": due_date,
        "pending_count": pending_count,
        "reviewer_count": reviewer_count,
        "reviewer_summary": reviewer_summary,
        "campaign_url": campaign_url,
        "auto_revoke_status": auto_revoke_status,
    }
    subject, body = _render_template(db, "certification_escalation", variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}
    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, addrs, bcc, mail_from, subject, html)


def send_approval_result(
    db: "Session",
    user_email: str,
    user_name: str,
    asset_type_name: str,
    approved: bool,
    approver_name: str = "",
    decline_reason: str | None = None,
) -> dict:
    """Sends approval granted or declined notification to the requester."""
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    app_title = get_config(db, "app.title", "ip·Solis")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    decline_reason_block = ""
    if not approved and decline_reason:
        decline_reason_block = f'<p><strong>Reason:</strong> {decline_reason}</p>'

    variables = {
        "company_name": company_name,
        "app_title": app_title,
        "requester_name": user_name,
        "requester_email": user_email,
        "asset_type_name": asset_type_name,
        "approver_name": approver_name,
        "decline_reason_block": decline_reason_block,
    }

    event_key = "approval_granted" if approved else "approval_declined"
    subject, body = _render_template(db, event_key, variables)
    if subject is None:
        return {"success": True, "skipped": True, "reason": "template inactive"}

    html = _build_branded_html(body, app_title, subject)
    return _send_html_email_multi(db, [user_email], bcc, mail_from, subject, html)


# ── RDP attachment helper ──────────────────────────────────────────────────────

def _make_rdp_content(hostname: str) -> bytes:
    """Generates a minimal .rdp file for a direct Remote Desktop connection."""
    return (
        f"full address:s:{hostname}\r\n"
        "prompt for credentials:i:1\r\n"
        "administrative session:i:0\r\n"
    ).encode("utf-8")


# ── SMTP helpers ───────────────────────────────────────────────────────────────

def _send_html_email_multi(
    db: "Session",
    recipients: list[str],
    bcc: str | None,
    mail_from: str,
    subject: str,
    html_body: str,
    rdp_hostname: str | None = None,
) -> dict:
    """Sends HTML email to multiple recipients (with optional BCC and RDP attachment)."""
    return _production_send_html_email(
        db, recipients, bcc, mail_from, subject, html_body,
        rdp_hostname=rdp_hostname,
    )


def _production_send_html_email(
    db: "Session",
    recipients: list[str],
    bcc: str | None,
    mail_from: str,
    subject: str,
    html_body: str,
    rdp_hostname: str | None = None,
) -> dict:
    import smtplib
    from email import encoders as email_encoders
    from email.mime.base import MIMEBase
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

    # Use multipart/mixed when an attachment is present so clients handle it correctly.
    # Structure: mixed → [ alternative → [ text/html ], application/x-rdp ]
    if rdp_hostname:
        msg = MIMEMultipart("mixed")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        rdp_part = MIMEBase("application", "x-rdp")
        rdp_part.set_payload(_make_rdp_content(rdp_hostname))
        email_encoders.encode_base64(rdp_part)
        rdp_part.add_header(
            "Content-Disposition", "attachment",
            filename=f"{rdp_hostname}.rdp",
        )
        msg.attach(rdp_part)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{mail_from}>"
    msg["To"] = ", ".join(recipients)
    if bcc:
        msg["Bcc"] = bcc

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_port == 587:
                server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.sendmail(mail_from, all_recipients, msg.as_string())
        logger.info("Email sent: to=%s subject=%r rdp=%s", recipients, subject, bool(rdp_hostname))
        return {"success": True, "to": recipients}
    except Exception as e:
        logger.error("Email failed: to=%s error=%s", recipients, e)
        return {"success": False, "error": str(e)}
