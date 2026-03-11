import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OrderAction(str, enum.Enum):
    PROVISION = "provision"
    MODIFY = "modify"
    EXTEND = "extend"
    DELETE = "delete"


class OrderStatus(str, enum.Enum):
    PENDING      = "pending"
    PROCESSING   = "processing"
    PROVISIONING = "provisioning"   # Worker started provision
    PROVISIONED  = "provisioned"    # Provision completed (active)
    DELIVERED    = "delivered"      # Legacy alias for PROVISIONED
    REVOKING     = "revoking"       # Worker started revoke
    REVOKED      = "revoked"        # Revoke completed
    FAILED       = "failed"
    EXPIRED      = "expired"
    CANCELLED    = "cancelled"


class StepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class Order(Base):
    """Orders and change requests."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ServiceNow-Referenznummer (optional, bei Portal-Bestellungen leer)
    servicenow_ref: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )

    # Benutzerinformationen – Besteller (Requestor)
    user_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Benutzerinformationen – Nutzer (Owner, kann vom Besteller abweichen)
    owner_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ServiceNow REQ number (servicenow_ref contains the RITM number)
    snow_req: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # Asset type and assigned machine
    asset_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("asset_types.id"), nullable=False
    )
    assigned_asset_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("asset_pool.id"), nullable=True
    )

    # RDP and admin users (array of email addresses / usernames)
    rdp_users: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    admin_users: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )

    # Zeitraum
    requested_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    requested_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Action and status
    action: Mapped[OrderAction] = mapped_column(
        Enum(OrderAction, name="order_action", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=OrderAction.PROVISION,
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=OrderStatus.PENDING,
        index=True,
    )

    # Celery task ID for status tracking
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Additional parameters as JSON (flexible extension)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Snapshot after successful provision (deterministic revoke)
    provisioned_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Fehlermeldung bei Status FAILED
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    asset_type: Mapped["AssetType"] = relationship(  # noqa: F821
        "AssetType", back_populates="orders"
    )
    assigned_asset: Mapped["AssetPool | None"] = relationship(  # noqa: F821
        "AssetPool",
        foreign_keys=[assigned_asset_id],
    )
    steps: Mapped[list["OrderStep"]] = relationship(
        "OrderStep", back_populates="order", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} user={self.user_email!r} "
            f"action={self.action} status={self.status}>"
        )


class OrderStep(Base):
    """Einzelne Modul-Schritte je Bestellung (Tracking)."""

    __tablename__ = "order_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=False, index=True
    )
    step_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[StepStatus] = mapped_column(
        Enum(StepStatus, name="step_status", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=StepStatus.PENDING,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    log_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    order: Mapped["Order"] = relationship("Order", back_populates="steps")

    def __repr__(self) -> str:
        return f"<OrderStep id={self.id} order={self.order_id} step={self.step_name!r} status={self.status}>"
