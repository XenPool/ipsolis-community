"""Add ad.use_ssl to app_config

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("""
            INSERT INTO app_config (key, value, description, is_secret, created_at, updated_at)
            VALUES ('ad.use_ssl', 'false', 'LDAPS – true für Port 636', false, NOW(), NOW())
            ON CONFLICT (key) DO NOTHING
        """)
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM app_config WHERE key = 'ad.use_ssl'"))
