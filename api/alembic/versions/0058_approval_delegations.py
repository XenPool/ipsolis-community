"""Approval delegations — route approvals to a deputy while the assignee is OOO.

When a new ``OrderApproval`` row is created, we look up active
delegations for the assigned approver. If one exists with a window
covering ``NOW()``, the approval is created against the delegate
instead, with a comment noting the original assignee. Existing
in-flight approvals are not retroactively re-routed.

Revision ID: 0058
Revises: 0057
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approval_delegations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # The user delegating (case-insensitive lookup is the caller's
        # responsibility — store as-given so the audit trail stays exact).
        sa.Column("approver_email", sa.String(255), nullable=False),
        sa.Column("approver_name", sa.String(255), nullable=True),
        sa.Column("delegate_email", sa.String(255), nullable=False),
        sa.Column("delegate_name", sa.String(255), nullable=True),
        sa.Column("from_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("until_at > from_at", name="ck_delegation_window"),
    )
    # Lookup index used on every approval-row creation; covers the
    # filter + range together so the planner gets a single index scan.
    op.create_index(
        "ix_approval_delegations_active",
        "approval_delegations",
        ["approver_email", "from_at", "until_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_approval_delegations_active", table_name="approval_delegations")
    op.drop_table("approval_delegations")
