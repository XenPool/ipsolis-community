"""Initial schema - alle 6 Kerntabellen

Revision ID: 0001
Revises:
Create Date: 2026-02-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ENUM as PgEnum

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Enums via raw SQL (no ORM auto-create magic) ──────────────────────────
    op.execute("CREATE TYPE asset_category AS ENUM ('vdi', 'server', 'workstation', 'other')")
    op.execute("CREATE TYPE asset_status AS ENUM ('free', 'reserved', 'busy', 'maintenance', 'reclaiming')")
    op.execute("CREATE TYPE order_action AS ENUM ('provision', 'modify', 'extend', 'delete')")
    op.execute("CREATE TYPE order_status AS ENUM ('pending', 'processing', 'delivered', 'failed', 'expired', 'cancelled')")
    op.execute("CREATE TYPE step_status AS ENUM ('pending', 'running', 'success', 'failed', 'skipped')")

    # ── asset_types ───────────────────────────────────────────────────────────
    op.create_table(
        "asset_types",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "category",
            PgEnum(name="asset_category", create_type=False),
            nullable=False,
            server_default="vdi",
        ),
        sa.Column("config", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # ── orders (vor asset_pool wegen Zirkular-FK) ─────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("servicenow_ref", sa.String(50), nullable=True),
        sa.Column("user_email", sa.String(255), nullable=False),
        sa.Column("user_name", sa.String(255), nullable=False),
        sa.Column("asset_type_id", sa.Integer(), nullable=False),
        sa.Column("assigned_asset_id", sa.Integer(), nullable=True),
        sa.Column("rdp_users", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("admin_users", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("requested_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "action",
            PgEnum(name="order_action", create_type=False),
            nullable=False,
            server_default="provision",
        ),
        sa.Column(
            "status",
            PgEnum(name="order_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("config", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["asset_type_id"], ["asset_types.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_servicenow_ref", "orders", ["servicenow_ref"])
    op.create_index("ix_orders_user_email", "orders", ["user_email"])
    op.create_index("ix_orders_requested_until", "orders", ["requested_until"])
    op.create_index("ix_orders_status", "orders", ["status"])

    # ── asset_pool ────────────────────────────────────────────────────────────
    op.create_table(
        "asset_pool",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("asset_type_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            PgEnum(name="asset_status", create_type=False),
            nullable=False,
            server_default="free",
        ),
        sa.Column("current_order_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reclaim_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["asset_type_id"], ["asset_types.id"]),
        sa.ForeignKeyConstraint(["current_order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_asset_pool_status", "asset_pool", ["status"])
    op.create_index("ix_asset_pool_expires_at", "asset_pool", ["expires_at"])

    # FK orders.assigned_asset_id -> asset_pool (nach asset_pool-Erstellung)
    op.create_foreign_key(
        "fk_orders_assigned_asset_id",
        "orders", "asset_pool",
        ["assigned_asset_id"], ["id"],
    )

    # ── order_steps ───────────────────────────────────────────────────────────
    op.create_table(
        "order_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            PgEnum(name="step_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("log_output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_steps_order_id", "order_steps", ["order_id"])

    # ── audit_log ─────────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("old_value", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("triggered_by", sa.String(255), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"])
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])
    op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])

    # ── app_config ────────────────────────────────────────────────────────────
    op.create_table(
        "app_config",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_app_config_key", "app_config", ["key"])


def downgrade() -> None:
    op.drop_table("app_config")
    op.drop_table("audit_log")
    op.drop_table("order_steps")
    op.drop_constraint("fk_orders_assigned_asset_id", "orders", type_="foreignkey")
    op.drop_table("asset_pool")
    op.drop_table("orders")
    op.drop_table("asset_types")
    op.execute("DROP TYPE IF EXISTS step_status")
    op.execute("DROP TYPE IF EXISTS order_status")
    op.execute("DROP TYPE IF EXISTS order_action")
    op.execute("DROP TYPE IF EXISTS asset_status")
    op.execute("DROP TYPE IF EXISTS asset_category")
