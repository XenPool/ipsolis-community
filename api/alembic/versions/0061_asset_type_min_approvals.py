"""Add min_approvals_required to asset_types (N-of-M threshold).

Default behaviour stays "all approvers must approve" — when the column
is ``NULL`` the runtime evaluator clamps the threshold to the total
number of approval rows for the order. Set it to N for N-of-M
semantics: any N of the configured approvers can satisfy the order,
remaining pending approvals are marked ``superseded`` (a new status
value, no DB-level enum constraint to update).

Decline semantics are unchanged: any single ``declined`` row
immediately rejects the order, regardless of N. This keeps a clear
veto path even for soft N-of-M policies.

Revision ID: 0061
Revises: 0060
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: Union[str, None] = "0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "asset_types",
        sa.Column("min_approvals_required", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("asset_types", "min_approvals_required")
