"""ORM for the approval_delegations table — see migration 0058."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ApprovalDelegation(Base):
    __tablename__ = "approval_delegations"
    __table_args__ = (
        CheckConstraint("until_at > from_at", name="ck_delegation_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    approver_email: Mapped[str] = mapped_column(String(255), nullable=False)
    approver_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delegate_email: Mapped[str] = mapped_column(String(255), nullable=False)
    delegate_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    from_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    until_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ApprovalDelegation id={self.id} {self.approver_email}→{self.delegate_email} "
            f"window={self.from_at}..{self.until_at}>"
        )
