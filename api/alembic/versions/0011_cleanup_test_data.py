"""Remove all seeded test data: orders, assets, runbooks, asset types, audit log

Revision ID: 0011
Revises: 0010
Create Date: 2026-02-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    # Delete in FK-dependency order (children before parents)
    conn.execute(sa.text("DELETE FROM order_steps"))
    conn.execute(sa.text("DELETE FROM order_change_log"))
    conn.execute(sa.text("DELETE FROM orders"))
    conn.execute(sa.text("DELETE FROM runbook_steps"))
    conn.execute(sa.text("DELETE FROM runbook_definitions"))
    conn.execute(sa.text("DELETE FROM asset_pool"))
    conn.execute(sa.text("DELETE FROM asset_types"))
    conn.execute(sa.text("DELETE FROM audit_log"))


def downgrade() -> None:
    # Data is gone – downgrade is a no-op
    pass
