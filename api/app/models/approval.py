import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"


class OrderApproval(Base):
    """Tracks individual approval decisions for orders that require manager or owner sign-off."""

    __tablename__ = "order_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    approver_type: Mapped[str] = mapped_column(String(30), nullable=False)  # 'manager' or 'application_owner'
    approver_email: Mapped[str] = mapped_column(String(255), nullable=False)
    approver_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Reminder tracking — populated by the approval-reminder Beat task.
    last_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Set the first time an approval is escalated; once non-NULL the Beat
    # task stops sending reminders + escalations for this row.
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Slice 2 of conditional approval rules: when an approval is created
    # by a matching rule, ``rule_name`` carries the full (un-truncated)
    # rule name (the ``approver_type`` column is capped at 30 chars and
    # only carries a short prefix). ``rule_threshold`` captures the
    # rule's per-rule N-of-M quorum at order-creation time so subsequent
    # admin edits to the asset-type rules don't shift the order's
    # decision logic mid-flight. NULL on both for static manager / owner
    # approvals.
    rule_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rule_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    order: Mapped["Order"] = relationship("Order", back_populates="approvals")  # noqa: F821

    def __repr__(self) -> str:
        return f"<OrderApproval id={self.id} order={self.order_id} type={self.approver_type} status={self.status}>"
