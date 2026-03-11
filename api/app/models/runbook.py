"""ORM models for dynamic runbooks."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RunbookDefinition(Base):
    """Ein Runbook pro Asset-Typ + Action."""

    __tablename__ = "runbook_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("asset_types.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(
        Enum(
            "provision", "modify", "extend", "delete",
            name="order_action",
            create_type=False,
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    steps: Mapped[list["RunbookStep"]] = relationship(
        "RunbookStep",
        back_populates="runbook",
        cascade="all, delete-orphan",
        order_by="RunbookStep.position",
    )
    asset_type: Mapped["Any"] = relationship("AssetType")  # noqa: F821

    def __repr__(self) -> str:
        return f"<RunbookDefinition id={self.id} name={self.name!r} action={self.action}>"


class RunbookStep(Base):
    """Geordneter Modul-Aufruf innerhalb eines Runbooks."""

    __tablename__ = "runbook_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    runbook_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("runbook_definitions.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    module_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    script_module_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("script_modules.id", ondelete="SET NULL"), nullable=True
    )
    params_template: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    runbook: Mapped["RunbookDefinition"] = relationship(
        "RunbookDefinition", back_populates="steps"
    )
    script_module: Mapped["Any"] = relationship("ScriptModule")  # noqa: F821

    def __repr__(self) -> str:
        ref = self.module_key or f"script_module:{self.script_module_id}"
        return (
            f"<RunbookStep id={self.id} runbook={self.runbook_id} "
            f"pos={self.position} module={ref!r}>"
        )
