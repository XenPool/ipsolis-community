"""Add approval_rules JSONB to asset_types — conditional approver routing.

Each rule is an object of shape::

    {
      "name":      "Long extension needs CISO",
      "condition": {"field": "duration_days", "op": ">",  "value": 90},
      "approvers": [{"email": "ciso@example.com", "name": "CISO"}]
    }

At order creation, ``app.utils.approval_rules.evaluate_rules`` walks
every rule, evaluates the condition against the order context, and
adds the rule's approvers as additional ``OrderApproval`` rows when
the condition matches. Existing manager / owner approvals continue to
work unchanged.

Slice 1 supports:
* fields:    duration_days, monthly_cost, has_pii, has_phi, has_pci, requester_department
* operators: >, >=, <, <=, ==, contains

A future slice can add boolean composition (AND / OR) by extending the
condition shape — the evaluator already centralises the logic in one
place so a richer language slots in without touching call sites.

Revision ID: 0064
Revises: 0063
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: Union[str, None] = "0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "asset_types",
        sa.Column("approval_rules", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("asset_types", "approval_rules")
