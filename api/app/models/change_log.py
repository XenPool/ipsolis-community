"""OrderChangeLog – immutable record of all changes made during provisioning.

Enables deterministic revoke: the revoke runner reads the entries
from this log and inverts exactly the actions performed during provisioning.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OrderChangeLog(Base):
    __tablename__ = "order_change_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)   # grant | revoke
    principal: Mapped[str] = mapped_column(String(255), nullable=False)  # user/email
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, name="metadata", nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    resolved_object_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<OrderChangeLog id={self.id} order={self.order_id}"
            f" {self.action} {self.principal} → {self.target_type}:{self.identifier!r}>"
        )
