import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models.asset import AssetType, AssignmentModel
from app.models.order import Order, OrderAction, OrderStatus
from app.schemas.order import OrderRead, WebhookPayload
from app.utils.audit import _order_snap, aaudit
from app.utils.capacity import enforce_pool_capacity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


def _verify_hmac(body: bytes, signature: str) -> bool:
    """Verifies HMAC-SHA256 signature from ServiceNow."""
    expected = hmac.new(
        settings.WEBHOOK_SECRET_TOKEN.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@router.post("/servicenow", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
async def receive_servicenow_webhook(
    request: Request,
    payload: WebhookPayload,
    db: AsyncSession = Depends(get_db),
    x_hub_signature_256: str | None = Header(default=None),
) -> Order:
    """
    Receives JSON webhooks from ServiceNow.

    ServiceNow sends:
    - X-Hub-Signature-256: sha256=<hmac>  (optional, but recommended)
    - JSON body according to WebhookPayload schema
    """
    # HMAC signature validation
    if not x_hub_signature_256:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Hub-Signature-256 header",
        )
    body = await request.body()
    if not _verify_hmac(body, x_hub_signature_256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # Resolve asset type by name
    result = await db.execute(
        select(AssetType).where(AssetType.name == payload.asset_type_name)
    )
    asset_type = result.scalar_one_or_none()
    if not asset_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown asset_type_name: {payload.asset_type_name!r}",
        )

    # Pre-flight capacity check — PROVISION only
    if (
        payload.action == OrderAction.PROVISION
        and asset_type.assignment_model == AssignmentModel.CAPACITY_POOLED
        and asset_type.pool_capacity is not None
    ):
        await enforce_pool_capacity(db, asset_type.id, asset_type.pool_capacity)

    # Check for duplicate ServiceNow reference (idempotency)
    existing = await db.execute(
        select(Order).where(Order.servicenow_ref == payload.servicenow_ref)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order with servicenow_ref {payload.servicenow_ref!r} already exists",
        )

    # Create order
    order = Order(
        servicenow_ref=payload.servicenow_ref,
        snow_req=payload.snow_req,
        user_email=str(payload.user_email),
        user_name=payload.user_name,
        owner_email=str(payload.owner_email) if payload.owner_email else None,
        owner_name=payload.owner_name,
        asset_type_id=asset_type.id,
        rdp_users=payload.rdp_users,
        admin_users=payload.admin_users,
        requested_from=payload.requested_from,
        requested_until=payload.requested_until,
        action=payload.action,
        status=OrderStatus.PENDING,
        config=payload.config,
    )
    db.add(order)
    await db.flush()  # generate ID without commit

    # Dispatch Celery task
    task_id = _dispatch_runbook(order)
    order.celery_task_id = task_id
    order.status = OrderStatus.PROCESSING

    await aaudit(
        db, "order", order.id, "created",
        new=_order_snap(order),
        by="api:servicenow_webhook",
        ctx=order.servicenow_ref,
    )
    await db.commit()

    # Re-fetch with relationships to avoid async lazy-load error
    result = await db.execute(
        select(Order).options(selectinload(Order.steps)).where(Order.id == order.id)
    )
    order = result.scalar_one()

    logger.info(
        "Webhook received: order_id=%s sn_ref=%s action=%s task=%s",
        order.id,
        order.servicenow_ref,
        order.action,
        task_id,
    )
    return order


def _dispatch_runbook(order: Order) -> str:
    """Dispatches the dynamic runbook task for the order.

    All actions run via dynamic_runner.run, which loads the appropriate
    runbook from the DB. Queue remains action-dependent.
    """
    from celery import Celery

    celery_app = Celery(broker=settings.CELERY_BROKER_URL)

    # DELETE/reclaim on separate queue for priority
    queue = "reclaim" if order.action == OrderAction.DELETE else "provision"
    result = celery_app.send_task(
        "tasks.workflows.dynamic_runner.run",
        args=[order.id],
        queue=queue,
    )
    return result.id
