"""ORM models for standalone runbooks (independent of asset types)."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class StandaloneRunbook(Base):
    __tablename__ = "standalone_runbooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    cron_expression: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cron_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    skip_if_running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    steps: Mapped[list["StandaloneRunbookStep"]] = relationship(
        "StandaloneRunbookStep",
        back_populates="runbook",
        cascade="all, delete-orphan",
        order_by="StandaloneRunbookStep.position",
    )
    runs: Mapped[list["StandaloneRunbookRun"]] = relationship(
        "StandaloneRunbookRun",
        back_populates="runbook",
        cascade="all, delete-orphan",
        order_by="StandaloneRunbookRun.id.desc()",
    )

    def __repr__(self) -> str:
        return f"<StandaloneRunbook id={self.id} name={self.name!r}>"


class StandaloneRunbookStep(Base):
    __tablename__ = "standalone_runbook_steps"
    __table_args__ = (
        UniqueConstraint("runbook_id", "position", name="uq_standalone_step_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    runbook_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("standalone_runbooks.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    script_module_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("script_modules.id", ondelete="SET NULL"), nullable=True
    )
    params_template: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_critical: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120, server_default="120")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    runbook: Mapped["StandaloneRunbook"] = relationship("StandaloneRunbook", back_populates="steps")
    script_module: Mapped["Any"] = relationship("ScriptModule")  # noqa: F821

    def __repr__(self) -> str:
        return f"<StandaloneRunbookStep id={self.id} pos={self.position} step={self.step_name!r}>"


class StandaloneRunbookRun(Base):
    __tablename__ = "standalone_runbook_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    runbook_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("standalone_runbooks.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    triggered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    runbook: Mapped["StandaloneRunbook"] = relationship("StandaloneRunbook", back_populates="runs")
    run_steps: Mapped[list["StandaloneRunbookRunStep"]] = relationship(
        "StandaloneRunbookRunStep",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="StandaloneRunbookRunStep.position",
    )

    def __repr__(self) -> str:
        return f"<StandaloneRunbookRun id={self.id} status={self.status!r}>"


class StandaloneRunbookRunStep(Base):
    __tablename__ = "standalone_runbook_run_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("standalone_runbook_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    log_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped["StandaloneRunbookRun"] = relationship("StandaloneRunbookRun", back_populates="run_steps")

    def __repr__(self) -> str:
        return f"<StandaloneRunbookRunStep id={self.id} step={self.step_name!r} status={self.status!r}>"
