from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from api.app.database import Base


class AuditLog(Base):
    """Unveränderliches Log aller Statusänderungen.

    Einträge werden nur INSERTed, nie UPDATEd oder DELETEd.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Welches Objekt wurde geändert?
    entity_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # z.B. "order", "asset", "order_step"
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # Was wurde getan?
    action: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # z.B. "status_changed", "created", "assigned"

    # Vorher / Nachher als JSON (flexible Struktur)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Wer/was hat die Änderung ausgelöst?
    triggered_by: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # z.B. "user:max@example.com", "celery:vdi_provision", "system:beat"

    # Zusätzlicher Kontext (z.B. Request-ID, Celery-Task-ID)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} entity={self.entity_type}:{self.entity_id} "
            f"action={self.action!r}>"
        )
