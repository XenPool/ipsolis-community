"""Add script_modules and global_vars tables; add script_module_id FK to runbook_steps

Revision ID: 0012
Revises: 0011
Create Date: 2026-02-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── script_modules ─────────────────────────────────────────────────────────
    op.create_table(
        "script_modules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("script_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("script_type", sa.String(20), nullable=False, server_default="powershell"),
        sa.Column("param_schema", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("NOW()")),
    )

    # ── global_vars ────────────────────────────────────────────────────────────
    op.create_table(
        "global_vars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False, unique=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("NOW()")),
    )

    # ── runbook_steps: add script_module_id FK, make module_key nullable ──────
    op.add_column(
        "runbook_steps",
        sa.Column(
            "script_module_id",
            sa.Integer(),
            sa.ForeignKey("script_modules.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.alter_column("runbook_steps", "module_key", nullable=True)


def downgrade() -> None:
    op.alter_column("runbook_steps", "module_key", nullable=False)
    op.drop_column("runbook_steps", "script_module_id")
    op.drop_table("global_vars")
    op.drop_table("script_modules")
