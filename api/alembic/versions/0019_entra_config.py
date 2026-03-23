"""Seed Entra ID / Azure AD SSO config keys

Revision ID: 0019
Revises: 0018
Create Date: 2026-03-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO app_config (key, value, description, is_secret) VALUES
        ('entra.mode',            'disabled', 'Portal SSO mode: disabled | entra_only | entra_with_onprem', false),
        ('entra.tenant_id',       '',         'Azure Tenant ID (GUID)',                                     false),
        ('entra.client_id',       '',         'App Registration Client ID (GUID)',                          false),
        ('entra.client_secret',   '',         'App Registration Client Secret',                             true),
        ('entra.redirect_uri',    '',         'OAuth2 callback URL (must match App Registration)',          false),
        ('entra.allowed_domains', '',         'Comma-separated UPN suffixes allowed to log in (blank = any)', false)
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM app_config WHERE key IN (
            'entra.mode', 'entra.tenant_id', 'entra.client_id',
            'entra.client_secret', 'entra.redirect_uri', 'entra.allowed_domains'
        )
    """)
