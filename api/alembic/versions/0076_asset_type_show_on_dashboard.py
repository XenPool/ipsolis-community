"""Add show_on_dashboard flag to asset_types — per-type donut on Admin Dashboard.

Operators check this on individual asset types via the Edit form to
opt-in to a status donut on the Admin Dashboard. Default ``false`` so
existing installs see no change until a superadmin/admin curates which
types belong on the dashboard. Helpdesk + auditor cannot toggle it
(system-wide visual, gated at the existing asset-type-write `admin`
role).

Revision ID: 0076
Revises: 0075
Create Date: 2026-04-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: Union[str, None] = "0075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "asset_types",
        sa.Column(
            "show_on_dashboard",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("asset_types", "show_on_dashboard")
