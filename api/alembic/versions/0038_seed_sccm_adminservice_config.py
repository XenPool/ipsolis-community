"""Seed SCCM Administration Service config keys

Revision ID: 0038
Revises: 0037
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO app_config (key, value, description, is_secret) VALUES
        ('sccm.base_url',   '', 'SCCM Admin Service base URL, e.g. https://sccm.example.com/AdminService', false),
        ('sccm.username',   '', 'SCCM service account (DOMAIN\\user)',                                      false),
        ('sccm.password',   '', 'SCCM service account password',                                            true),
        ('sccm.verify_tls', 'true', 'Verify TLS certificate (true/false)',                                  false),
        ('sccm.site_code',  '', 'Primary site code, e.g. P01',                                              false)
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM app_config WHERE key IN (
            'sccm.base_url',
            'sccm.username',
            'sccm.password',
            'sccm.verify_tls',
            'sccm.site_code'
        )
    """)
