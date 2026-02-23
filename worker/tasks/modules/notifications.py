"""Modul: Notifications – E-Mail-Benachrichtigungen.

Entspricht dem Ivanti-Modul 'Send-Notification'.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
MAIL_FROM = os.getenv("MAIL_FROM", "noreply@example.com")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "XenPool IT Selfservice")
REMINDER_HOURS = int(os.getenv("REMINDER_HOURS_BEFORE_EXPIRY", "24"))


def send_provision_confirmation(
    user_email: str,
    user_name: str,
    asset_name: str,
    rdp_users: list[str],
    expires_at: datetime,
) -> dict:
    """Sendet Bereitstellungsbestätigung an den User."""
    subject = f"Ihre VDI '{asset_name}' wurde bereitgestellt"
    body = f"""
Hallo {user_name},

Ihre virtuelle Maschine wurde erfolgreich bereitgestellt:

  VM-Name:     {asset_name}
  RDP-Zugang:  {', '.join(rdp_users) if rdp_users else '(keiner)'}
  Verfügbar bis: {expires_at.strftime('%d.%m.%Y %H:%M')} Uhr

Bitte verwenden Sie die Remote-Desktop-Verbindung mit dem Servernamen '{asset_name}'.

Mit freundlichen Grüßen
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
    subject = f"Erinnerung: Ihre VDI '{asset_name}' läuft in {int(hours_remaining)}h ab"
    body = f"""
Hallo {user_name},

Ihre virtuelle Maschine läuft bald ab:

  VM-Name:     {asset_name}
  Ablauf:      {expires_at.strftime('%d.%m.%Y %H:%M')} Uhr
  Verbleibend: ca. {int(hours_remaining)} Stunden

Falls Sie die VM länger benötigen, verlängern Sie die Laufzeit bitte
im IT Self-Service-Portal vor dem Ablauftermin.

Mit freundlichen Grüßen
XenPool IT Selfservice
    """.strip()

    return _send_email(user_email, subject, body)


def send_reclaim_notification(
    user_email: str,
    user_name: str,
    asset_name: str,
) -> dict:
    """Benachrichtigt User über Rückführung der VM in den Pool."""
    subject = f"Ihre VDI '{asset_name}' wurde zurückgegeben"
    body = f"""
Hallo {user_name},

Ihre virtuelle Maschine '{asset_name}' wurde in den Pool zurückgegeben
und wird jetzt neu aufgesetzt.

Falls Sie eine neue VM benötigen, bestellen Sie diese gerne erneut
im IT Self-Service-Portal.

Mit freundlichen Grüßen
XenPool IT Selfservice
    """.strip()

    return _send_email(user_email, subject, body)


def send_expiry_reminders() -> dict:
    """
    Celery Beat Task: Sendet Erinnerungsmails für bald ablaufende Assets.
    Wird von Celery Beat periodisch aufgerufen.
    """
    if ENVIRONMENT == "development":
        logger.info("[MOCK] Checking for assets expiring within %sh ...", REMINDER_HOURS)
        logger.info("[MOCK] No expiring assets found (mock mode)")
        return {"sent": 0, "mock": True}

    # Production: DB-Abfrage und Mail-Versand
    # (wird im nächsten Sprint implementiert)
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
