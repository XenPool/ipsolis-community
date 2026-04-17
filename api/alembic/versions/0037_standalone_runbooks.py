"""Add standalone runbook tables

Revision ID: 0037
Revises: 0036
Create Date: 2026-04-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "standalone_runbooks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("cron_expression", sa.String(100), nullable=True),
        sa.Column("cron_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("skip_if_running", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "standalone_runbook_steps",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("runbook_id", sa.Integer, sa.ForeignKey("standalone_runbooks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("step_name", sa.String(255), nullable=False),
        sa.Column("script_module_id", sa.Integer, sa.ForeignKey("script_modules.id", ondelete="SET NULL"), nullable=True),
        sa.Column("params_template", sa.JSON, nullable=True),
        sa.Column("is_critical", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="3"),
        sa.Column("timeout_seconds", sa.Integer, nullable=False, server_default="120"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("runbook_id", "position", name="uq_standalone_step_position"),
    )

    op.create_table(
        "standalone_runbook_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("runbook_id", sa.Integer, sa.ForeignKey("standalone_runbooks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("triggered_by", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "standalone_runbook_run_steps",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("standalone_runbook_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_name", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("log_output", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("standalone_runbook_run_steps")
    op.drop_table("standalone_runbook_runs")
    op.drop_table("standalone_runbook_steps")
    op.drop_table("standalone_runbooks")
