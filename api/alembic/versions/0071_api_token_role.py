"""Bind API tokens to a role — RBAC slice 3.

Adds an optional ``role`` column to ``api_tokens``. Semantics:

* NULL → no role assigned. Pre-slice-3 behaviour: scopes alone govern
  what the token can do. ``require_role`` continues to bypass for
  tokens (back-compat for existing integrations).
* set → token is treated as that role for ``require_role`` checks,
  in addition to its scopes. A token issued with ``role=approver``
  is blocked from ``/admin/maintenance/*`` even if it carries
  ``admin:*`` scope.

The mintable-role guard (a non-superadmin can't issue a superadmin
token) lives in app code at create-time, not in this migration.

Revision ID: 0071
Revises: 0070
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: Union[str, None] = "0070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_tokens",
        sa.Column("role", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_tokens", "role")
