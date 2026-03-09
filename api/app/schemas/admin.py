import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from app.models.asset import AssetCategory, AssetStatus


# ── Attribut-Typ-System ────────────────────────────────────────────────────────

class AttributeType(str, enum.Enum):
    STRING = "STRING"
    INT = "INT"
    BOOL = "BOOL"
    ENUM = "ENUM"
    MULTI_ENUM = "MULTI_ENUM"


class AttributeDefinition(BaseModel):
    key: str
    label: str
    type: AttributeType = AttributeType.STRING
    required: bool = False
    default_value: Any = None
    options: list[str] | None = None
    validation: dict[str, Any] | None = None
    visible_when: dict[str, Any] | None = None

    @model_validator(mode="after")
    def options_required_for_enum(self) -> "AttributeDefinition":
        if self.type in (AttributeType.ENUM, AttributeType.MULTI_ENUM):
            if not self.options:
                raise ValueError(f"Attribute '{self.key}': options required for type {self.type}")
        return self


# ── app_config ─────────────────────────────────────────────────────────────────

class AppConfigRead(BaseModel):
    id: int
    key: str
    value: str | None       # None / "***" wenn is_secret=True
    description: str | None
    is_secret: bool
    updated_at: datetime

    model_config = {"from_attributes": True}


class AppConfigCreate(BaseModel):
    key: str
    value: str
    description: str | None = None
    is_secret: bool = False


class AppConfigUpdate(BaseModel):
    value: str
    description: str | None = None


# ── Asset-Typen ────────────────────────────────────────────────────────────────

class AssetTypeCreate(BaseModel):
    name: str
    description: str | None = None
    category: AssetCategory = AssetCategory.PLATFORM_ACCESS
    config: list[dict[str, Any]] | None = None
    assignment_model: str = "assigned_personal"
    pool_capacity: int | None = None
    automation_mode: str = "runbook"
    targets: list[dict[str, Any]] | None = None
    lifecycle_ttl_days: int | None = None
    lifecycle_renewable: bool = True
    deprovision_policy: str = "access_only"
    personal_provisioning_strategy: str | None = None
    naming_pattern: str | None = None
    max_per_user: int = 1
    automation_strategy: str = "runbook_only"
    composite_steps: list[dict[str, Any]] | None = None
    # Constraint validation: IDs of existing runbooks (not persisted, validation-time only)
    runbook_provision_id: int | None = None
    runbook_revoke_id: int | None = None


class AssetTypeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: AssetCategory | None = None
    config: list[dict[str, Any]] | None = None
    assignment_model: str | None = None
    pool_capacity: int | None = None
    automation_mode: str | None = None
    targets: list[dict[str, Any]] | None = None
    lifecycle_ttl_days: int | None = None
    lifecycle_renewable: bool | None = None
    deprovision_policy: str | None = None
    personal_provisioning_strategy: str | None = None
    naming_pattern: str | None = None
    max_per_user: int | None = None
    automation_strategy: str | None = None
    composite_steps: list[dict[str, Any]] | None = None
    # Constraint validation: IDs of existing runbooks (not persisted, validation-time only)
    runbook_provision_id: int | None = None
    runbook_revoke_id: int | None = None


# ── Asset-Pool ─────────────────────────────────────────────────────────────────

class AssetPoolCreate(BaseModel):
    name: str
    asset_type_id: int
    status: AssetStatus = AssetStatus.FREE
    asset_metadata: dict[str, Any] | None = None


class AssetPoolUpdate(BaseModel):
    status: AssetStatus | None = None
    asset_metadata: dict[str, Any] | None = None
    expires_at: datetime | None = None


# ── Audit-Log ──────────────────────────────────────────────────────────────────

class AuditLogRead(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    action: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    triggered_by: str
    context: str | None
    timestamp: datetime

    model_config = {"from_attributes": True}
