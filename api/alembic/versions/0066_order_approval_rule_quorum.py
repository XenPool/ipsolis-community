"""Add per-rule N-of-M quorum tracking to order_approvals.

Slice 2 of the conditional approval rules feature lets each rule carry
its own ``min_approvals_required`` threshold. When set, the rule's
approvers form their own quorum group at decision time — independent
of the asset-type-level threshold that covers manager / owner / no-
threshold-rule approvers as one pool.

Two new columns:

* ``rule_name`` — full (un-truncated) name of the rule that produced
  the approval row, used to group rule-driven approvals at decision
  time. NULL for static manager / owner rows.
* ``rule_threshold`` — integer N ≥ 1 captured at order creation; NULL
  means "no per-rule quorum, fold into the global pool". Capturing it
  on the row (rather than re-reading the rule definition at decision
  time) freezes the threshold against subsequent admin edits to
  ``approval_rules`` — the order's quorum doesn't shift mid-flight.

Revision ID: 0066
Revises: 0065
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: Union[str, None] = "0065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "order_approvals",
        sa.Column("rule_name", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "order_approvals",
        sa.Column("rule_threshold", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("order_approvals", "rule_threshold")
    op.drop_column("order_approvals", "rule_name")
