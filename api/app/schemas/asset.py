from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.asset import AssetCategory, AssetStatus


class AssetTypeRead(BaseModel):
    id: int
    name: str
    description: str | None
    category: AssetCategory
    config: dict[str, Any] | None
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
    asset_metadata: dict[str, Any] | None = Field(None, alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}
