"""Add ps_modules table for admin-managed PowerShell module installations

Revision ID: 0013
Revises: 0012
Create Date: 2026-03-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE ps_modules (
            id               SERIAL PRIMARY KEY,
            name             VARCHAR(100) NOT NULL UNIQUE,
            required_version VARCHAR(50),
            status           VARCHAR(20) NOT NULL DEFAULT 'pending',
            installed_version VARCHAR(50),
            error_log        TEXT,
            created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS ps_modules"))
