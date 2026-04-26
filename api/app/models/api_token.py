"""ORM for the api_tokens table — see migration 0054."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # RBAC slice 3: optional role binding. NULL = scope-only authz
    # (pre-slice-3 behaviour, back-compat for existing integrations).
    # When set, ``require_role`` checks the token's role on routes that
    # gate by role; scope checks still apply independently.
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)

    def __repr__(self) -> str:
        return f"<ApiToken id={self.id} name={self.name!r} prefix={self.token_prefix!r}>"
