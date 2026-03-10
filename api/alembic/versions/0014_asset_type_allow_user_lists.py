"""Add allow_user_lists column to asset_types

Revision ID: 0014
Revises: 0013
Create Date: 2026-03-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE asset_types
        ADD COLUMN allow_user_lists BOOLEAN NOT NULL DEFAULT FALSE
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE asset_types
        DROP COLUMN allow_user_lists
    """))
