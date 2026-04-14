"""Seed app_config with placeholder values for SMTP, AD, company

Revision ID: 0003
Revises: 0002
Create Date: 2026-02-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SEED_ROWS = [
    # (key, value, description, is_secret)
    # ── E-Mail ────────────────────────────────────────────────────────────────
    ("email.smtp_server",  "localhost",              "SMTP-Server Hostname",                  False),
    ("email.smtp_port",    "25",                     "SMTP-Port (25=plain, 587=STARTTLS)",     False),
    ("email.from",         "noreply@example.com",    "Absender-Adresse",                      False),
    ("email.bcc",          "it@example.com",         "BCC-Empfänger für alle Systemmails",    False),
    ("email.username",     "",                       "SMTP-Benutzername (leer = kein Auth)",  False),
    ("email.password",     "",                       "SMTP-Passwort",                         True),
    # ── Active Directory ──────────────────────────────────────────────────────
    ("ad.server",          "dc.example.com",         "LDAP-Server (Domain Controller)",       False),
    ("ad.port",            "389",                    "LDAP-Port (389=plain, 636=SSL)",         False),
    ("ad.base_dn",         "DC=example,DC=com",      "LDAP Base DN für Benutzersuche",        False),
    ("ad.domain",          "EXAMPLE",                "NetBIOS-Domainname für Bind",           False),
    ("ad.username",        "svc_vdi",                "Service-Account für LDAP-Bind",         False),
    ("ad.password",        "",                       "Passwort des Service-Accounts",         True),
    # ── Allgemein ─────────────────────────────────────────────────────────────
    ("company.name",       "XenPool",                "Firmenname für E-Mail-Templates",       False),
]


def upgrade() -> None:
    conn = op.get_bind()
    for key, value, description, is_secret in _SEED_ROWS:
        conn.execute(
            sa.text("""
                INSERT INTO app_config (key, value, description, is_secret, created_at, updated_at)
                VALUES (:key, :value, :description, :is_secret, NOW(), NOW())
                ON CONFLICT (key) DO NOTHING
            """),
            {"key": key, "value": value, "description": description, "is_secret": is_secret},
        )


def downgrade() -> None:
    conn = op.get_bind()
    keys = [row[0] for row in _SEED_ROWS]
    conn.execute(
        sa.text("DELETE FROM app_config WHERE key = ANY(:keys)"),
        {"keys": keys},
    )
