"""Add 'scheduled' value to order_status enum + seed portal.max_advance_days config

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-09
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 'scheduled' to the existing order_status enum
    op.execute("ALTER TYPE order_status ADD VALUE IF NOT EXISTS 'scheduled'")

    # Seed portal.max_advance_days config (default 30 days)
    op.execute("""
        INSERT INTO app_config (key, value, description, is_secret, created_at, updated_at)
        VALUES (
            'portal.max_advance_days',
            '30',
            'Maximum number of days in advance a user can schedule an order start date (0 = no limit)',
            false,
            NOW(),
            NOW()
        )
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; manual cleanup required
    op.execute("DELETE FROM app_config WHERE key = 'portal.max_advance_days'")
