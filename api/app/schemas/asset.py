from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.asset import AssetCategory, AssetStatus


class AssetTypeRead(BaseModel):
    id: int
    name: str
    description: str | None
    help_text: str | None = None
    is_active: bool = True
    show_on_dashboard: bool = False
    category: AssetCategory
    config: list[dict[str, Any]] | None
    assignment_model: str
    pool_capacity: int | None
    automation_mode: str
    targets: list[dict[str, Any]] | None
    lifecycle_ttl_days: int | None
    lifecycle_renewable: bool
    lifecycle_reminder_days: int | None
    allow_rdp_users: bool
    allow_admin_users: bool
    deprovision_policy: str
    personal_provisioning_strategy: str | None
    naming_pattern: str | None
    max_per_user: int
    monthly_cost: float | None = None
    currency: str | None = None
    cost_center: str | None = None
    automation_strategy: str
    composite_steps: list[dict[str, Any]] | None
    rds_gateway_url: str | None
    requires_manager_approval: bool
    requires_owner_approval: bool
    approval_owners: list[dict[str, Any]] | None
    approval_rules: list[dict[str, Any]] | None = None
    min_approvals_required: int | None = None
    requires_approval_on_modify: bool
    eligible_requestors_dn: str | None
    logo: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AssetPoolRead(BaseModel):
    id: int
    name: str
    asset_type_id: int
    status: AssetStatus
    current_order_id: int | None
    expires_at: datetime | None
    last_reclaim_at: datetime | None
    # ORM attribute is "asset_metadata"; serialise as "metadata" in JSON responses
    asset_metadata: dict[str, Any] | None = Field(None, serialization_alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}
