"""Modul: Notifications – E-Mail-Benachrichtigungen.

Entspricht dem Ivanti-Modul 'Send-Notification'.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
MAIL_FROM = os.getenv("MAIL_FROM", "noreply@example.com")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "XenPool IT Selfservice")
REMINDER_HOURS = int(os.getenv("REMINDER_HOURS_BEFORE_EXPIRY", "24"))


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
    """
    Sends bilingual order confirmation (DE/EN) to requester and optionally owner.

    Konfiguration wird aus der app_config-Tabelle gelesen:
      email.from, email.bcc, email.smtp_server, email.smtp_port,
      email.username, email.password, company.name
    """
    from tasks.modules.config_reader import get_config

    company_name = get_config(db, "company.name", "XenPool")
    mail_from = get_config(db, "email.from", MAIL_FROM)
    bcc = get_config(db, "email.bcc")

    # Owner defaults to requester if no owner specified
    effective_owner_email = owner_email or user_email
    effective_owner_name = owner_name or user_name

    # Recipients: requester + owner (if different)
    recipients = [user_email]
    if owner_email and owner_email.lower() != user_email.lower():
        recipients.append(owner_email)

    from_date = requested_from.strftime("%d.%m.%Y")
    until_date = requested_until.strftime("%d.%m.%Y")

    subject = (
        f"[{company_name}] Bestellbestätigung / Order Confirmation"
        + (f" – {snow_ritm}" if snow_ritm else "")
    )

    html_body = _build_order_confirmation_html(
        company_name=company_name,
        user_name=user_name,
        user_email=user_email,
        owner_name=effective_owner_name,
        owner_email=effective_owner_email,
        asset_type_name=asset_type_name,
        asset_type_description=asset_type_description or "",
        from_date=from_date,
        until_date=until_date,
        snow_req=snow_req or "",
        snow_ritm=snow_ritm or "",
    )

    return _send_html_email_multi(db, recipients, bcc, mail_from, subject, html_body)


def _build_order_confirmation_html(
    company_name: str,
    user_name: str,
    user_email: str,
    owner_name: str,
    owner_email: str,
    asset_type_name: str,
    asset_type_description: str,
    from_date: str,
    until_date: str,
    snow_req: str,
    snow_ritm: str,
) -> str:
    """Erstellt zweisprachiges HTML-E-Mail-Template (DE/EN)."""
    BRAND_COLOR = "#BB0A30"

    snow_row_de = ""
    snow_row_en = ""
    if snow_req or snow_ritm:
        snow_row_de = f"""
        <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">REQ / RITM:</td>
            <td style="padding:4px 8px;">{snow_req} / {snow_ritm}</td></tr>"""
        snow_row_en = f"""
        <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">REQ / RITM:</td>
            <td style="padding:4px 8px;">{snow_req} / {snow_ritm}</td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:20px 0;">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:4px;overflow:hidden;">

      <!-- Header -->
      <tr>
        <td colspan="2" style="background:{BRAND_COLOR};padding:24px 32px;">
          <div style="color:#ffffff;font-size:22px;font-weight:bold;">{company_name} IT Self-Service</div>
          <div style="color:#ffffff;font-size:14px;margin-top:4px;opacity:0.9;">
            Bestellbestätigung &nbsp;|&nbsp; Order Confirmation
          </div>
        </td>
      </tr>

      <!-- Body: DE | EN side by side -->
      <tr>
        <!-- Deutsch -->
        <td width="50%" valign="top" style="padding:24px 20px 24px 32px;border-right:2px solid #eeeeee;">
          <h2 style="color:{BRAND_COLOR};font-size:16px;margin:0 0 12px;">Deutsch</h2>
          <p style="margin:0 0 16px;">Hallo {user_name},</p>
          <p style="margin:0 0 16px;">
            Ihre Bestellung wurde erfolgreich aufgenommen und wird nun bearbeitet.
          </p>
          <table cellpadding="0" cellspacing="0" style="width:100%;font-size:13px;">
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Typ:</td>
                <td style="padding:4px 8px;font-weight:bold;">{asset_type_name}</td></tr>
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Beschreibung:</td>
                <td style="padding:4px 8px;">{asset_type_description}</td></tr>
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Zeitraum:</td>
                <td style="padding:4px 8px;">{from_date} – {until_date}</td></tr>
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Besteller:</td>
                <td style="padding:4px 8px;">{user_name} &lt;{user_email}&gt;</td></tr>
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Nutzer:</td>
                <td style="padding:4px 8px;">{owner_name} &lt;{owner_email}&gt;</td></tr>
            {snow_row_de}
          </table>
          <p style="margin:16px 0 0;font-size:12px;color:#888;">
            Sie erhalten eine weitere Benachrichtigung sobald die VM bereitgestellt wurde.
          </p>
        </td>

        <!-- English -->
        <td width="50%" valign="top" style="padding:24px 32px 24px 20px;">
          <h2 style="color:{BRAND_COLOR};font-size:16px;margin:0 0 12px;">English</h2>
          <p style="margin:0 0 16px;">Hello {user_name},</p>
          <p style="margin:0 0 16px;">
            Your order has been successfully submitted and is now being processed.
          </p>
          <table cellpadding="0" cellspacing="0" style="width:100%;font-size:13px;">
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Type:</td>
                <td style="padding:4px 8px;font-weight:bold;">{asset_type_name}</td></tr>
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Description:</td>
                <td style="padding:4px 8px;">{asset_type_description}</td></tr>
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Period:</td>
                <td style="padding:4px 8px;">{from_date} – {until_date}</td></tr>
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Requestor:</td>
                <td style="padding:4px 8px;">{user_name} &lt;{user_email}&gt;</td></tr>
            <tr><td style="padding:4px 8px;color:#555;white-space:nowrap;">Owner:</td>
                <td style="padding:4px 8px;">{owner_name} &lt;{owner_email}&gt;</td></tr>
            {snow_row_en}
          </table>
          <p style="margin:16px 0 0;font-size:12px;color:#888;">
            You will receive another notification once the VM has been provisioned.
          </p>
        </td>
      </tr>

      <!-- Footer -->
      <tr>
        <td colspan="2" style="background:#f8f8f8;padding:16px 32px;border-top:1px solid #eeeeee;">
          <p style="margin:0;font-size:11px;color:#aaa;text-align:center;">
            {company_name} IT Self-Service &nbsp;|&nbsp; Diese E-Mail wurde automatisch generiert / This email was generated automatically.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def _send_html_email_multi(
    db: "Session",
    recipients: list[str],
    bcc: str | None,
    mail_from: str,
    subject: str,
    html_body: str,
) -> dict:
    """Sends HTML email to multiple recipients (with optional BCC)."""
    if ENVIRONMENT == "development":
        return _mock_send_html_email(recipients, bcc, subject, html_body)

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

    all_recipients = list(recipients)
    if bcc:
        all_recipients.append(bcc)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{MAIL_FROM_NAME} <{mail_from}>"
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
        logger.info("HTML email sent: to=%s subject=%r", recipients, subject)
        return {"success": True, "to": recipients}
    except Exception as e:
        logger.error("HTML email failed: to=%s error=%s", recipients, e)
        return {"success": False, "error": str(e)}


def _mock_send_html_email(recipients: list[str], bcc: str | None, subject: str, html_body: str) -> dict:
    logger.info(
        "[MOCK] HTML Email would be sent:\n"
        "  To:      %s\n"
        "  Bcc:     %s\n"
        "  Subject: %s\n"
        "  Body:    %s chars (HTML)",
        recipients, bcc or "(none)", subject, len(html_body),
    )
    return {"success": True, "to": recipients, "mock": True}


def send_provision_confirmation(
    user_email: str,
    user_name: str,
    asset_name: str,
    rdp_users: list[str],
    expires_at: datetime,
) -> dict:
    """Sends provisioning confirmation to the user."""
    subject = f"Ihre VDI '{asset_name}' wurde bereitgestellt"
    body = f"""
Hallo {user_name},

Ihre virtuelle Maschine wurde erfolgreich bereitgestellt:

  VM-Name:     {asset_name}
  RDP-Zugang:  {', '.join(rdp_users) if rdp_users else '(keiner)'}
  Available until: {expires_at.strftime('%d.%m.%Y %H:%M')}

Bitte verwenden Sie die Remote-Desktop-Verbindung mit dem Servernamen '{asset_name}'.

Kind regards
XenPool IT Selfservice
    """.strip()

    return _send_email(user_email, subject, body)


def send_expiry_reminder(
    user_email: str,
    user_name: str,
    asset_name: str,
    expires_at: datetime,
    hours_remaining: float,
) -> dict:
    """Sendet Ablauf-Erinnerungsmail."""
    subject = f"Reminder: Your VDI '{asset_name}' expires in {int(hours_remaining)}h"
    body = f"""
Hallo {user_name},

Your virtual machine is expiring soon:

  VM-Name:     {asset_name}
  Ablauf:      {expires_at.strftime('%d.%m.%Y %H:%M')} Uhr
  Verbleibend: ca. {int(hours_remaining)} Stunden

If you need the VM for longer, please extend the duration
im IT Self-Service-Portal vor dem Ablauftermin.

Kind regards
XenPool IT Selfservice
    """.strip()

    return _send_email(user_email, subject, body)


def send_reclaim_notification(
    user_email: str,
    user_name: str,
    asset_name: str,
) -> dict:
    """Notifies user about VM being returned to the pool."""
    subject = f"Your VDI '{asset_name}' has been returned"
    body = f"""
Hallo {user_name},

Your virtual machine '{asset_name}' has been returned to the pool
und wird jetzt neu aufgesetzt.

If you need a new VM, feel free to order again
im IT Self-Service-Portal.

Kind regards
XenPool IT Selfservice
    """.strip()

    return _send_email(user_email, subject, body)


def send_expiry_reminders() -> dict:
    """
    Celery Beat task: sends reminder emails for expiring assets.
    Wird von Celery Beat periodisch aufgerufen.
    """
    if ENVIRONMENT == "development":
        logger.info("[MOCK] Checking for assets expiring within %sh ...", REMINDER_HOURS)
        logger.info("[MOCK] No expiring assets found (mock mode)")
        return {"sent": 0, "mock": True}

    # Production: DB query and email dispatch
    # (to be implemented in the next sprint)
    return {"sent": 0}


def _send_email(to_email: str, subject: str, body: str) -> dict:
    """Sendet eine E-Mail (Production: SMTP, Development: Log-Mock)."""
    if ENVIRONMENT == "development":
        return _mock_send_email(to_email, subject, body)

    return _production_send_email(to_email, subject, body)


def _production_send_email(to_email: str, subject: str, body: str) -> dict:
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.getenv("SMTP_HOST", "localhost")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_tls = os.getenv("SMTP_TLS", "true").lower() == "true"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
    msg["To"] = to_email

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_tls:
                server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.sendmail(MAIL_FROM, [to_email], msg.as_string())
        logger.info("Email sent: to=%s subject=%r", to_email, subject)
        return {"success": True, "to": to_email}
    except Exception as e:
        logger.error("Email failed: to=%s error=%s", to_email, e)
        return {"success": False, "error": str(e)}


def _mock_send_email(to_email: str, subject: str, body: str) -> dict:
    logger.info(
        "[MOCK] Email would be sent:\n"
        "  To:      %s\n"
        "  From:    %s <%s>\n"
        "  Subject: %s\n"
        "  Body:    %s",
        to_email, MAIL_FROM_NAME, MAIL_FROM, subject, body[:100] + "...",
    )
    return {"success": True, "to": to_email, "mock": True}
