"""Add admin_users table for RBAC slice 1.

Replaces the binary "X-Admin-Key OR session = god mode" model with a
proper user table backed by per-user passwords (PBKDF2-SHA256, stdlib
hashing — no external password library required) and a five-tier role
ladder:

    superadmin > admin > approver > auditor > helpdesk

The legacy ``ADMIN_API_KEY`` continues to work as a back-compat fallback
mapped to ``superadmin``, so existing scripts and integrations don't
break on upgrade. New per-user logins layer on top.

Slice 1 is intentionally small: the table is created here, the login
flow + first-run-setup wires up in app code, and role gates are
applied to a curated set of endpoints (audit-log viewer, asset-type
CRUD, admin-user CRUD itself). Comprehensive role gating across the
rest of ``/admin/*``, per-asset-type ACLs, and SoD enforcement
(configurer ≠ approver) are slice-2 work.

Revision ID: 0069
Revises: 0068
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: Union[str, None] = "0068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # username is normalised to lowercase at write time; the unique
        # index is therefore reliable without funcidx-LOWER acrobatics.
        sa.Column("username", sa.String(length=128), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        # Free-form attribution (string from ``actor_by`` or ``first-run-setup``)
        # so the trail of "who created which admin user" survives even when
        # the audit log entry is rotated out by retention.
        sa.Column("created_by", sa.String(length=255), nullable=False),
    )
    op.create_index(
        "ix_admin_users_username", "admin_users", ["username"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_admin_users_username", table_name="admin_users")
    op.drop_table("admin_users")
