import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.config import settings
from api.app.database import get_db
from api.app.models.asset import AssetType
from api.app.models.order import Order, OrderAction, OrderStatus
from api.app.schemas.order import OrderRead, WebhookPayload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


def _verify_hmac(body: bytes, signature: str) -> bool:
    """Verifiziert HMAC-SHA256-Signatur von ServiceNow."""
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
    Empfängt JSON-Webhooks von ServiceNow.

    ServiceNow schickt:
    - X-Hub-Signature-256: sha256=<hmac>  (optional, aber empfohlen)
    - JSON-Body gemäß WebhookPayload-Schema
    """
    # HMAC-Validierung (in Production erzwingen)
    if not settings.is_development:
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

    # Asset-Typ nach Name auflösen
    result = await db.execute(
        select(AssetType).where(AssetType.name == payload.asset_type_name)
    )
    asset_type = result.scalar_one_or_none()
    if not asset_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown asset_type_name: {payload.asset_type_name!r}",
        )

    # Doppelte ServiceNow-Referenz prüfen (Idempotenz)
    existing = await db.execute(
        select(Order).where(Order.servicenow_ref == payload.servicenow_ref)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order with servicenow_ref {payload.servicenow_ref!r} already exists",
        )

    # Order anlegen
    order = Order(
        servicenow_ref=payload.servicenow_ref,
        user_email=str(payload.user_email),
        user_name=payload.user_name,
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
    await db.flush()  # ID generieren ohne commit

    # Celery-Task dispatchen
    task_id = _dispatch_runbook(order)
    order.celery_task_id = task_id
    order.status = OrderStatus.PROCESSING

    await db.commit()
    await db.refresh(order)

    logger.info(
        "Webhook received: order_id=%s sn_ref=%s action=%s task=%s",
        order.id,
        order.servicenow_ref,
        order.action,
        task_id,
    )
    return order


def _dispatch_runbook(order: Order) -> str:
    """Dispatcht den passenden Celery-Runbook-Task für die Order."""
    from celery import Celery

    celery_app = Celery(broker=settings.CELERY_BROKER_URL)

    task_map = {
        OrderAction.PROVISION: "tasks.workflows.vdi_provision.run",
        OrderAction.MODIFY: "tasks.workflows.vdi_modify.run",
        OrderAction.EXTEND: "tasks.workflows.vdi_modify.run",
        OrderAction.DELETE: "tasks.workflows.vdi_reclaim.run",
    }

    task_name = task_map.get(order.action, "tasks.workflows.vdi_provision.run")
    result = celery_app.send_task(task_name, args=[order.id])
    return result.id
