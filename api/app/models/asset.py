import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.app.database import Base


class AssetCategory(str, enum.Enum):
    VDI = "vdi"
    SERVER = "server"
    WORKSTATION = "workstation"
    OTHER = "other"


class AssetStatus(str, enum.Enum):
    FREE = "free"
    RESERVED = "reserved"
    BUSY = "busy"
    MAINTENANCE = "maintenance"
    RECLAIMING = "reclaiming"


class AssetType(Base):
    """Typdefinitionen – z.B. 'Test VDI', 'Business VDI'."""

    __tablename__ = "asset_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[AssetCategory] = mapped_column(
        Enum(AssetCategory, name="asset_category"),
        nullable=False,
        default=AssetCategory.VDI,
    )
    # Flexible Konfiguration: RAM, CPU, Disk, etc. als JSON
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
    assets: Mapped[list["AssetPool"]] = relationship(
        "AssetPool", back_populates="asset_type"
    )
    orders: Mapped[list["Order"]] = relationship(  # noqa: F821
        "Order", back_populates="asset_type"
    )

    def __repr__(self) -> str:
        return f"<AssetType id={self.id} name={self.name!r}>"


class AssetPool(Base):
    """Alle verwalteten Assets/VMs."""

    __tablename__ = "asset_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    asset_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("asset_types.id"), nullable=False
    )
    status: Mapped[AssetStatus] = mapped_column(
        Enum(AssetStatus, name="asset_status"),
        nullable=False,
        default=AssetStatus.FREE,
        index=True,
    )
    current_order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_reclaim_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # VM-spezifische Metadaten: vSphere-Objekt-ID, Hostname, IP, etc.
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
    asset_type: Mapped["AssetType"] = relationship(
        "AssetType", back_populates="assets"
    )
    current_order: Mapped["Order | None"] = relationship(  # noqa: F821
        "Order",
        foreign_keys=[current_order_id],
        back_populates="assigned_asset",
    )

    def __repr__(self) -> str:
        return f"<AssetPool id={self.id} name={self.name!r} status={self.status}>"
