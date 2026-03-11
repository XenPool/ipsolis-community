import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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

from app.database import Base


class AssetCategory(str, enum.Enum):
    APPLICATION_ACCESS = "application_access"
    PLATFORM_ACCESS = "platform_access"
    DATA_ACCESS = "data_access"
    DEVICE_ACCESS = "device_access"
    INFRASTRUCTURE_ACCESS = "infrastructure_access"


class AssignmentModel(str, enum.Enum):
    CAPACITY_POOLED = "capacity_pooled"       # Pool/quota, no dedicated instance (RDS, SaaS license)
    DEDICATED_SHARED = "dedicated_shared"     # Instance exists, no fixed owner (jump host)
    ASSIGNED_PERSONAL = "assigned_personal"   # Instance 1:1 user-owned (personal VDI, laptop)


class DeprovisionPolicy(str, enum.Enum):
    ACCESS_ONLY = "access_only"                   # Remove group membership only
    RETURN_TO_POOL = "return_to_pool"             # Release pool reservation, instance remains free
    DEALLOCATE_INSTANCE = "deallocate_instance"   # Stop / deallocate VM
    DELETE_INSTANCE = "delete_instance"           # Delete VM including cleanup
    CUSTOM_RUNBOOK = "custom_runbook"             # Revoke via separate runbook


class PersonalProvisioningStrategy(str, enum.Enum):
    ASSIGN_EXISTING_FREE = "assign_existing_free"       # Assign free instance from pool
    CREATE_NEW = "create_new"                            # Create new instance (stub MVP)
    REUSE_EXISTING_BY_OWNER = "reuse_existing_by_owner" # User already has an instance → reuse it


class AutomationStrategy(str, enum.Enum):
    GROUP_ONLY = "group_only"       # Group targets only (formerly targets_only)
    RUNBOOK_ONLY = "runbook_only"   # Runbook only (formerly runbook)
    COMPOSITE = "composite"         # Group targets + runbook in configurable order


class AssetStatus(str, enum.Enum):
    FREE = "free"
    RESERVED = "reserved"
    BUSY = "busy"
    MAINTENANCE = "maintenance"
    RECLAIMING = "reclaiming"


class AssetType(Base):
    """Type definitions – e.g. 'Test VDI', 'Business VDI'."""

    __tablename__ = "asset_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[AssetCategory] = mapped_column(
        Enum(AssetCategory, name="asset_category", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=AssetCategory.PLATFORM_ACCESS,
    )
    # Structured attributes: [{"key": "cpu", "label": "CPU count", "options": ["2", "4", "8"]}]
    config: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # Assignment model: capacity_pooled / dedicated_shared / assigned_personal
    assignment_model: Mapped[str] = mapped_column(
        String(30), nullable=False, default="assigned_personal", server_default="assigned_personal"
    )
    pool_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Automation: targets_only (config-driven group membership) or runbook
    automation_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="runbook", server_default="runbook"
    )
    # Targets: [{"type": "ad_group", "identifier": "CN=...", "principal_source": "requester"}]
    targets: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # New automation strategy (replaces automation_mode long-term)
    automation_strategy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="runbook_only", server_default="runbook_only"
    )
    # composite_steps: [{"type": "GROUP_TARGETS", "order": 1}, {"type": "RUNBOOK", "order": 2}]
    composite_steps: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    # Deprovision behavior on revoke
    deprovision_policy: Mapped[str] = mapped_column(
        String(30), nullable=False, default="access_only", server_default="access_only"
    )
    # Personal assignment: how is the instance provisioned?
    personal_provisioning_strategy: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    # Naming pattern for new instances, e.g. "VDI-{sam}"
    naming_pattern: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Max. gleichzeitige Instanzen pro User
    max_per_user: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Lifecycle
    lifecycle_ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lifecycle_renewable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    allow_user_lists: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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
        Enum(AssetStatus, name="asset_status", values_callable=lambda x: [e.value for e in x]),
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
    # Note: "metadata" is reserved by SQLAlchemy Declarative; column name stays "metadata"
    asset_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, name="metadata", nullable=True
    )
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
    )

    def __repr__(self) -> str:
        return f"<AssetPool id={self.id} name={self.name!r} status={self.status}>"
