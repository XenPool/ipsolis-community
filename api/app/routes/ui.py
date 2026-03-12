"""Admin UI – Server-Side Rendered HTML via Jinja2 + HTMX."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.asset import AssetPool, AssetStatus, AssetType
from app.models.global_var import GlobalVar
from app.models.ps_module import PsModule
from app.models.order import Order, OrderAction, OrderStatus
from app.models.runbook import RunbookDefinition, RunbookStep
from app.models.script_module import ScriptModule
from app.utils.auth import require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ui",
    tags=["ui"],
    dependencies=[Depends(require_admin_key)],
)
templates = Jinja2Templates(directory="/app/app/templates")

# ── Helpers ───────────────────────────────────────────────────────────────────

_STATUS_COLORS = {
    "pending":    "bg-gray-100 text-gray-700",
    "processing": "bg-blue-100 text-blue-700",
    "delivered":  "bg-green-100 text-green-700",
    "failed":     "bg-red-100 text-red-700",
    "expired":    "bg-orange-100 text-orange-700",
    "cancelled":  "bg-gray-100 text-gray-500",
}

_STEP_COLORS = {
    "pending": "bg-gray-100 text-gray-600",
    "running": "bg-blue-100 text-blue-700",
    "success": "bg-green-100 text-green-700",
    "failed":  "bg-red-100 text-red-700",
    "skipped": "bg-gray-100 text-gray-400",
}

_ASSET_STATUS_COLORS = {
    "free":        "bg-green-100 text-green-700",
    "reserved":    "bg-yellow-100 text-yellow-700",
    "busy":        "bg-blue-100 text-blue-700",
    "reclaiming":  "bg-orange-100 text-orange-700",
    "maintenance": "bg-gray-100 text-gray-600",
}


async def _pool_summary(db: AsyncSession) -> dict:
    """Returns pool status counts."""
    rows = await db.execute(
        select(AssetPool.status, func.count().label("cnt"))
        .group_by(AssetPool.status)
    )
    counts = {row.status.value: row.cnt for row in rows}
    total = sum(counts.values())
    return {
        "free":        counts.get("free", 0),
        "busy":        counts.get("busy", 0),
        "reclaiming":  counts.get("reclaiming", 0),
        "maintenance": counts.get("maintenance", 0),
        "reserved":    counts.get("reserved", 0),
        "total":       total,
    }


# ── HTMX Fragment: Pool Summary ───────────────────────────────────────────────

@router.get("/_pool-summary", response_class=HTMLResponse)
async def pool_summary_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """HTMX-Fragment: Pool-Status-Karten (auto-refreshed every 30s)."""
    summary = await _pool_summary(db)
    return templates.TemplateResponse(
        request, "fragments/pool_summary.html",
        {"summary": summary, "asset_status_colors": _ASSET_STATUS_COLORS},
    )


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    summary = await _pool_summary(db)

    # Last 20 orders
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.steps))
        .order_by(Order.created_at.desc())
        .limit(20)
    )
    recent_orders = result.scalars().all()

    # Asset name lookup for assigned assets
    asset_ids = [o.assigned_asset_id for o in recent_orders if o.assigned_asset_id]
    asset_names: dict[int, str] = {}
    if asset_ids:
        asset_rows = await db.execute(
            select(AssetPool.id, AssetPool.name).where(AssetPool.id.in_(asset_ids))
        )
        asset_names = {row.id: row.name for row in asset_rows}

    # Assignment model lookup for shared/pooled assets
    type_ids = list({o.asset_type_id for o in recent_orders if o.asset_type_id})
    asset_type_models: dict[int, str] = {}
    if type_ids:
        type_rows = await db.execute(
            select(AssetType.id, AssetType.assignment_model).where(AssetType.id.in_(type_ids))
        )
        asset_type_models = {row.id: row.assignment_model for row in type_rows}

    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "summary": summary,
            "recent_orders": recent_orders,
            "asset_names": asset_names,
            "asset_type_models": asset_type_models,
            "status_colors": _STATUS_COLORS,
            "asset_status_colors": _ASSET_STATUS_COLORS,
            "now": datetime.now(timezone.utc),
        },
    )


# ── Orders List ───────────────────────────────────────────────────────────────

@router.get("/orders", response_class=HTMLResponse)
async def orders_list(
    request: Request,
    status_filter: str | None = None,
    user_email: str | None = None,
    page: int = 1,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    limit = 50
    offset = (page - 1) * limit

    query = select(Order).options(selectinload(Order.steps))
    if status_filter:
        try:
            query = query.where(Order.status == OrderStatus(status_filter))
        except ValueError:
            pass
    if user_email:
        query = query.where(Order.user_email.ilike(f"%{user_email}%"))

    query = query.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    orders = result.scalars().all()

    # Count total for pagination
    count_query = select(func.count()).select_from(Order)
    if status_filter:
        try:
            count_query = count_query.where(Order.status == OrderStatus(status_filter))
        except ValueError:
            pass
    if user_email:
        count_query = count_query.where(Order.user_email.ilike(f"%{user_email}%"))
    total_count = (await db.execute(count_query)).scalar_one()

    # Asset name lookup (for personal assets with assigned_asset_id)
    asset_ids = [o.assigned_asset_id for o in orders if o.assigned_asset_id]
    asset_names: dict[int, str] = {}
    if asset_ids:
        asset_rows = await db.execute(
            select(AssetPool.id, AssetPool.name).where(AssetPool.id.in_(asset_ids))
        )
        asset_names = {row.id: row.name for row in asset_rows}

    # Assignment model lookup (for shared/pooled assets without assigned_asset_id)
    type_ids = list({o.asset_type_id for o in orders if o.asset_type_id})
    asset_type_models: dict[int, str] = {}
    if type_ids:
        type_rows = await db.execute(
            select(AssetType.id, AssetType.assignment_model).where(AssetType.id.in_(type_ids))
        )
        asset_type_models = {row.id: row.assignment_model for row in type_rows}

    return templates.TemplateResponse(
        request, "orders.html",
        {
            "orders": orders,
            "asset_names": asset_names,
            "asset_type_models": asset_type_models,
            "status_colors": _STATUS_COLORS,
            "status_filter": status_filter or "",
            "user_email": user_email or "",
            "page": page,
            "total_count": total_count,
            "limit": limit,
            "has_prev": page > 1,
            "has_next": offset + limit < total_count,
            "all_statuses": [s.value for s in OrderStatus],
        },
    )


# ── Order Detail ──────────────────────────────────────────────────────────────

@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(
    request: Request,
    order_id: int,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.steps))
        .where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order {order_id} not found")

    # Asset name
    asset_name = None
    if order.assigned_asset_id:
        asset_row = await db.execute(
            select(AssetPool.name).where(AssetPool.id == order.assigned_asset_id)
        )
        asset_name = asset_row.scalar_one_or_none()

    # Asset type (for allow_user_lists flag in admin actions)
    asset_type = await db.get(AssetType, order.asset_type_id) if order.asset_type_id else None

    # Compute step durations
    steps_with_duration = []
    for step in sorted(order.steps, key=lambda s: s.id):
        duration = None
        if step.started_at and step.finished_at:
            delta = step.finished_at - step.started_at.replace(
                tzinfo=step.finished_at.tzinfo
            ) if step.finished_at.tzinfo and not step.started_at.tzinfo else (
                step.finished_at - step.started_at
            )
            duration = f"{delta.total_seconds():.1f}s"
        steps_with_duration.append({"step": step, "duration": duration})

    return templates.TemplateResponse(
        request, "order_detail.html",
        {
            "order": order,
            "asset_name": asset_name,
            "asset_type": asset_type,
            "steps_with_duration": steps_with_duration,
            "status_colors": _STATUS_COLORS,
            "step_colors": _STEP_COLORS,
        },
    )


# ── Admin Order Actions ────────────────────────────────────────────────────────

@router.post("/orders/{order_id}/change")
async def admin_change_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    new_until: str | None = Form(default=None),
    rdp_users: list[str] = Form(default=[]),
    admin_users: list[str] = Form(default=[]),
):
    """Admin: change order on behalf of user (duration + user lists)."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")

    if original.status not in (OrderStatus.DELIVERED, OrderStatus.PROVISIONED):
        raise HTTPException(
            status_code=422,
            detail="Only active orders (DELIVERED/PROVISIONED) can be modified",
        )

    if new_until:
        try:
            requested_until = datetime.fromisoformat(new_until).replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
    else:
        requested_until = original.requested_until

    # Textarea sends newline-separated values as a single string; split them
    rdp_clean = [u.strip() for line in rdp_users for u in line.splitlines() if u.strip()]
    admin_clean = [u.strip() for line in admin_users for u in line.splitlines() if u.strip()]

    if original.assigned_asset_id and requested_until != original.requested_until:
        asset = await db.get(AssetPool, original.assigned_asset_id)
        if asset:
            asset.expires_at = requested_until

    new_order = Order(
        user_email=original.user_email,
        user_name=original.user_name,
        owner_email=original.owner_email,
        owner_name=original.owner_name,
        asset_type_id=original.asset_type_id,
        assigned_asset_id=original.assigned_asset_id,
        rdp_users=rdp_clean,
        admin_users=admin_clean,
        requested_from=original.requested_from,
        requested_until=requested_until,
        action=OrderAction.MODIFY,
        status=OrderStatus.PENDING,
    )
    db.add(new_order)
    await db.flush()

    from app.routes.webhook import _dispatch_runbook
    new_order.celery_task_id = _dispatch_runbook(new_order)
    new_order.status = OrderStatus.PROCESSING
    await db.commit()

    logger.info("Admin: Change order id=%s from order=%s", new_order.id, order_id)
    return RedirectResponse(url=f"/ui/orders/{new_order.id}", status_code=303)


@router.post("/orders/{order_id}/cancel")
async def admin_cancel_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Admin: Bestellung im Namen des Users abbestellen (DELETE)."""
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")

    if original.status not in (OrderStatus.DELIVERED, OrderStatus.PROVISIONED):
        raise HTTPException(
            status_code=422,
            detail="Only active orders (DELIVERED/PROVISIONED) can be cancelled",
        )

    cancel_order = Order(
        user_email=original.user_email,
        user_name=original.user_name,
        owner_email=original.owner_email,
        owner_name=original.owner_name,
        asset_type_id=original.asset_type_id,
        assigned_asset_id=original.assigned_asset_id,
        rdp_users=original.rdp_users,
        admin_users=original.admin_users,
        requested_from=original.requested_from,
        requested_until=original.requested_until,
        action=OrderAction.DELETE,
        status=OrderStatus.PENDING,
        provisioned_state=original.provisioned_state,
    )
    db.add(cancel_order)
    await db.flush()

    from app.routes.webhook import _dispatch_runbook
    cancel_order.celery_task_id = _dispatch_runbook(cancel_order)
    cancel_order.status = OrderStatus.PROCESSING
    await db.commit()

    logger.info("Admin: Cancel order id=%s from order=%s", cancel_order.id, order_id)
    return RedirectResponse(url=f"/ui/orders/{cancel_order.id}", status_code=303)


# ── Asset Types UI ─────────────────────────────────────────────────────────────

@router.get("/asset-types", response_class=HTMLResponse)
async def asset_types_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await db.execute(select(AssetType).order_by(AssetType.name))
    asset_types = result.scalars().all()

    # Runbook-Counts je Asset-Typ
    rb_counts: dict[int, int] = {}
    if asset_types:
        rows = await db.execute(
            text("""
                SELECT asset_type_id, COUNT(*) as cnt
                FROM runbook_definitions
                WHERE asset_type_id = ANY(:ids)
                GROUP BY asset_type_id
            """),
            {"ids": [t.id for t in asset_types]},
        )
        rb_counts = {row[0]: row[1] for row in rows}

    # Pool counts per asset type
    pool_counts: dict[int, dict] = {}
    if asset_types:
        count_rows = (await db.execute(
            text("SELECT asset_type_id, status, count(*) FROM asset_pool GROUP BY asset_type_id, status"),
        )).all()
        for r in count_rows:
            tid = r[0]
            pool_counts.setdefault(tid, {"free": 0, "total": 0})
            pool_counts[tid]["total"] += r[2]
            if r[1] == "free":
                pool_counts[tid]["free"] += r[2]

    return templates.TemplateResponse(
        request, "ui/asset_types.html",
        {
            "asset_types": asset_types,
            "rb_counts": rb_counts,
            "pool_counts": pool_counts,
            "active_page": "asset-types",
        },
    )


@router.get("/asset-pool", response_class=HTMLResponse)
async def asset_pool_page(
    request: Request,
    asset_type_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    types_result = await db.execute(select(AssetType).order_by(AssetType.name))
    asset_types = types_result.scalars().all()
    return templates.TemplateResponse(
        request, "ui/asset_pool.html",
        {
            "asset_types": asset_types,
            "selected_type_id": asset_type_id,
            "active_page": "asset-pool",
        },
    )


@router.get("/asset-types/new", response_class=HTMLResponse)
async def asset_type_new_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "ui/asset_type_form.html",
        {"asset_type": None, "active_page": "asset-types"},
    )


@router.get("/asset-types/{type_id}/edit", response_class=HTMLResponse)
async def asset_type_edit_form(
    request: Request,
    type_id: int,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    t = await db.get(AssetType, type_id)
    if not t:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request, "ui/asset_type_form.html",
        {"asset_type": t, "active_page": "asset-types"},
    )


# ── Runbooks UI ────────────────────────────────────────────────────────────────

_ACTIONS = ["provision", "modify", "extend", "delete"]


@router.get("/runbooks", response_class=HTMLResponse)
async def runbooks_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    at_result = await db.execute(select(AssetType).order_by(AssetType.name))
    asset_types = at_result.scalars().all()

    rb_result = await db.execute(
        select(RunbookDefinition)
        .options(selectinload(RunbookDefinition.steps))
        .order_by(RunbookDefinition.asset_type_id, RunbookDefinition.action)
    )
    runbooks = rb_result.scalars().all()

    # Grouped: {asset_type_id: {action: runbook}}
    rb_by_type: dict[int, dict] = {t.id: {} for t in asset_types}
    for rb in runbooks:
        rb_by_type.setdefault(rb.asset_type_id, {})[rb.action] = rb

    return templates.TemplateResponse(
        request, "ui/runbooks.html",
        {
            "asset_types": asset_types,
            "rb_by_type": rb_by_type,
            "actions": _ACTIONS,
            "active_page": "runbooks",
        },
    )


@router.get("/runbooks/{runbook_id}/edit", response_class=HTMLResponse)
async def runbook_editor(
    request: Request,
    runbook_id: int,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await db.execute(
        select(RunbookDefinition)
        .options(selectinload(RunbookDefinition.steps))
        .where(RunbookDefinition.id == runbook_id)
    )
    rb = result.scalar_one_or_none()
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    at = await db.get(AssetType, rb.asset_type_id)

    # Load script modules from DB for step editor dropdown
    mod_result = await db.execute(
        select(ScriptModule).where(ScriptModule.is_active.is_(True)).order_by(ScriptModule.name)
    )
    script_modules = mod_result.scalars().all()

    # Serialize steps to plain dicts so Jinja2 tojson works
    steps_data = [
        {
            "id": s.id,
            "position": s.position,
            "step_name": s.step_name,
            "module_key": s.module_key,
            "script_module_id": s.script_module_id,
            "params_template": s.params_template or {},
            "is_critical": s.is_critical,
            "retry_count": s.retry_count,
            "timeout_seconds": s.timeout_seconds,
        }
        for s in rb.steps
    ]

    return templates.TemplateResponse(
        request, "ui/runbook_editor.html",
        {
            "runbook": rb,
            "steps": steps_data,
            "asset_type": at,
            "script_modules": script_modules,
            "active_page": "runbooks",
        },
    )


@router.get("/_module-params", response_class=HTMLResponse)
async def module_params_fragment(
    request: Request,
    module_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """HTMX fragment: param fields for the selected script module."""
    module = await db.get(ScriptModule, module_id) if module_id else None
    return templates.TemplateResponse(
        request, "ui/fragments/module_params.html",
        {"module": module},
    )


# ── Script Modules UI ──────────────────────────────────────────────────────────

@router.get("/modules", response_class=HTMLResponse)
async def modules_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await db.execute(select(ScriptModule).order_by(ScriptModule.name))
    modules = result.scalars().all()
    return templates.TemplateResponse(
        request, "ui/modules.html",
        {"modules": modules, "active_page": "modules"},
    )


@router.get("/modules/new", response_class=HTMLResponse)
async def module_new_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "ui/module_editor.html",
        {"module": None, "active_page": "modules"},
    )


@router.get("/modules/{module_id}/edit", response_class=HTMLResponse)
async def module_edit_form(
    request: Request,
    module_id: int,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    module = await db.get(ScriptModule, module_id)
    if not module:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request, "ui/module_editor.html",
        {"module": module, "active_page": "modules"},
    )


# ── PS Modules UI ──────────────────────────────────────────────────────────────

@router.get("/ps-modules", response_class=HTMLResponse)
async def ps_modules_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await db.execute(select(PsModule).order_by(PsModule.name))
    ps_modules = result.scalars().all()
    return templates.TemplateResponse(
        request, "ui/ps_modules.html",
        {"ps_modules": ps_modules, "active_page": "ps-modules"},
    )


# ── Global Variables UI ────────────────────────────────────────────────────────

@router.get("/global-vars", response_class=HTMLResponse)
async def global_vars_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    result = await db.execute(select(GlobalVar).order_by(GlobalVar.key))
    vars_list = result.scalars().all()
    _SECRET_MASK = "***"
    masked = [
        {
            "id": v.id,
            "key": v.key,
            "value": _SECRET_MASK if v.is_secret else (v.value or ""),
            "description": v.description or "",
            "is_secret": v.is_secret,
            "updated_at": v.updated_at,
        }
        for v in vars_list
    ]
    return templates.TemplateResponse(
        request, "ui/global_vars.html",
        {"vars": masked, "active_page": "global-vars"},
    )
