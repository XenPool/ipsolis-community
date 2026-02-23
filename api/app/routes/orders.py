import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.app.config import settings
from api.app.database import get_db
from api.app.models.order import Order, OrderStatus
from api.app.schemas.order import OrderCreate, OrderRead, OrderUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/", response_model=list[OrderRead])
async def list_orders(
    user_email: str | None = None,
    status_filter: OrderStatus | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[Order]:
    """Gibt alle Bestellungen zurück (optional gefiltert)."""
    query = select(Order).options(selectinload(Order.steps))

    if user_email:
        query = query.where(Order.user_email == user_email)
    if status_filter:
        query = query.where(Order.status == status_filter)

    query = query.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/{order_id}", response_model=OrderRead)
async def get_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
) -> Order:
    """Gibt eine einzelne Bestellung mit allen Schritten zurück."""
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.steps))
        .where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {order_id} not found",
        )
    return order


@router.post("/", response_model=OrderRead, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    db: AsyncSession = Depends(get_db),
) -> Order:
    """
    Erstellt eine neue Bestellung via Self-Service-Portal.
    (Für ServiceNow-Webhooks: POST /webhook/servicenow)
    """
    order = Order(
        user_email=str(payload.user_email),
        user_name=payload.user_name,
        asset_type_id=payload.asset_type_id,
        rdp_users=payload.rdp_users,
        admin_users=payload.admin_users,
        requested_from=payload.requested_from,
        requested_until=payload.requested_until,
        action=payload.action,
        status=OrderStatus.PENDING,
        config=payload.config,
    )
    db.add(order)
    await db.flush()

    # Celery-Task dispatchen
    from api.app.routes.webhook import _dispatch_runbook
    task_id = _dispatch_runbook(order)
    order.celery_task_id = task_id
    order.status = OrderStatus.PROCESSING

    await db.commit()
    await db.refresh(order)

    logger.info("Order created: id=%s user=%s action=%s", order.id, order.user_email, order.action)
    return order


@router.patch("/{order_id}", response_model=OrderRead)
async def update_order(
    order_id: int,
    payload: OrderUpdate,
    db: AsyncSession = Depends(get_db),
) -> Order:
    """Aktualisiert eine bestehende Bestellung (z.B. User-Änderung, Verlängerung)."""
    result = await db.execute(
        select(Order).options(selectinload(Order.steps)).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {order_id} not found",
        )

    if payload.rdp_users is not None:
        order.rdp_users = payload.rdp_users
    if payload.admin_users is not None:
        order.admin_users = payload.admin_users
    if payload.requested_until is not None:
        order.requested_until = payload.requested_until
    if payload.status is not None:
        order.status = payload.status
    if payload.error_message is not None:
        order.error_message = payload.error_message

    await db.commit()
    await db.refresh(order)
    return order


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Markiert eine Bestellung als cancelled (kein physisches Löschen)."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {order_id} not found",
        )

    if order.status in (OrderStatus.DELIVERED, OrderStatus.PROCESSING):
        # Bei PROCESSING: Reclaim-Runbook triggern
        from api.app.routes.webhook import _dispatch_runbook
        from api.app.models.order import OrderAction
        order.action = OrderAction.DELETE
        _dispatch_runbook(order)

    order.status = OrderStatus.CANCELLED
    await db.commit()
