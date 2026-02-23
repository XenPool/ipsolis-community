from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, field_validator

from api.app.models.order import OrderAction, OrderStatus, StepStatus


# ── OrderStep ─────────────────────────────────────────────────────────────────

class OrderStepRead(BaseModel):
    id: int
    order_id: int
    step_name: str
    status: StepStatus
    started_at: datetime | None
    finished_at: datetime | None
    log_output: str | None
    error: str | None

    model_config = {"from_attributes": True}


# ── Order ─────────────────────────────────────────────────────────────────────

class OrderCreate(BaseModel):
    user_email: EmailStr
    user_name: str
    asset_type_id: int
    rdp_users: list[str] = []
    admin_users: list[str] = []
    requested_from: datetime
    requested_until: datetime
    action: OrderAction = OrderAction.PROVISION
    config: dict[str, Any] | None = None

    @field_validator("requested_until")
    @classmethod
    def until_after_from(cls, v: datetime, info: Any) -> datetime:
        if "requested_from" in info.data and v <= info.data["requested_from"]:
            raise ValueError("requested_until must be after requested_from")
        return v


class OrderUpdate(BaseModel):
    rdp_users: list[str] | None = None
    admin_users: list[str] | None = None
    requested_until: datetime | None = None
    status: OrderStatus | None = None
    error_message: str | None = None


class OrderRead(BaseModel):
    id: int
    servicenow_ref: str | None
    user_email: str
    user_name: str
    asset_type_id: int
    assigned_asset_id: int | None
    rdp_users: list[str]
    admin_users: list[str]
    requested_from: datetime
    requested_until: datetime
    action: OrderAction
    status: OrderStatus
    celery_task_id: str | None
    config: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    steps: list[OrderStepRead] = []

    model_config = {"from_attributes": True}


# ── ServiceNow Webhook Payload ────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    """JSON-Payload den ServiceNow an /webhook schickt."""

    servicenow_ref: str
    action: OrderAction
    user_email: EmailStr
    user_name: str
    asset_type_name: str  # wird zu asset_type_id aufgelöst
    rdp_users: list[str] = []
    admin_users: list[str] = []
    requested_from: datetime
    requested_until: datetime
    config: dict[str, Any] | None = None
