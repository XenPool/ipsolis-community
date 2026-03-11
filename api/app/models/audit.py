from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    """Immutable log of all status changes.

    Entries are only INSERTed, never UPDATEd or DELETEd.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Which object was changed?
    entity_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # e.g. "order", "asset", "order_step"
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # What was done?
    action: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # z.B. "status_changed", "created", "assigned"

    # Vorher / Nachher als JSON (flexible Struktur)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Who/what triggered the change?
    triggered_by: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # z.B. "user:max@example.com", "celery:vdi_provision", "system:beat"

    # Additional context (e.g. request ID, Celery task ID)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} entity={self.entity_type}:{self.entity_id} "
            f"action={self.action!r}>"
        )
