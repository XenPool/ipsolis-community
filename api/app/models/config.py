from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from api.app.database import Base


class AppConfig(Base):
    """Zentrale Variablenverwaltung – Ivanti-Äquivalent.

    Konfigurationswerte die zur Laufzeit geändert werden können,
    ohne den Container neu zu starten.
    Secrets werden verschlüsselt gespeichert (is_secret=True).
    """

    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_secret: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # Wenn True → Wert wird in der UI maskiert
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        display = "***" if self.is_secret else repr(self.value)
        return f"<AppConfig key={self.key!r} value={display}>"
