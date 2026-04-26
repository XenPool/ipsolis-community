"""Add monthly_cost / currency / cost_center to asset_types.

Drives the chargeback / cost report — finance can see projected monthly
spend per cost center based on active orders. All three columns are
nullable; existing definitions remain untracked until an admin fills
them in.

Revision ID: 0056
Revises: 0055
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "asset_types",
        sa.Column("monthly_cost", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "asset_types",
        sa.Column("currency", sa.String(3), nullable=True),
    )
    op.add_column(
        "asset_types",
        sa.Column("cost_center", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("asset_types", "cost_center")
    op.drop_column("asset_types", "currency")
    op.drop_column("asset_types", "monthly_cost")
