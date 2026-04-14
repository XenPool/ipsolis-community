"""User Self-Service Portal – HTML routes.

Authentication: Entra ID SSO (entra.mode must be set to 'enabled' via Admin > Settings).
"""
import logging
from datetime import date, datetime, timedelta, timezone

import base64

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import or_, select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sqlalchemy import func as sa_func

from app.config import settings
from app.database import get_db
from app.templates_instance import templates, get_app_logo
from app.models.asset import AssetPool, AssetStatus, AssetType
from app.models.config import AppConfig
from app.models.order import Order, OrderAction, OrderStatus
from app.utils.ad_lookup import lookup_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portal", tags=["portal"])


@router.get("/logo", include_in_schema=False)
async def portal_logo() -> Response:
    """Serves the portal logo image from the in-memory cache (set at startup / config save).
    Returns 404 when no logo is configured. Browser-cacheable for 1 hour.
    """
    data_url = get_app_logo()
    if not data_url:
        raise HTTPException(status_code=404, detail="No logo configured")
    try:
        # data URL format: data:<mime>;base64,<b64data>
        header, b64_data = data_url.split(",", 1)
        mime = header.split(":")[1].split(";")[0]
        raw = base64.b64decode(b64_data)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid logo data")
    return Response(content=raw, media_type=mime, headers={"Cache-Control": "max-age=3600"})


def _user_order_filter(email: str):
    """Returns a SQLAlchemy filter that matches orders belonging to the given user
    (either as requester or as the asset owner)."""
    return or_(Order.user_email == email, Order.owner_email == email)


def _assert_owns_order(order: Order, email: str) -> None:
    """Raises HTTP 403 if the current user is not the requester or owner of the order."""
    if order.user_email != email and order.owner_email != email:
        raise HTTPException(status_code=403, detail="Access denied")


async def require_portal_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """FastAPI dependency: returns the authenticated portal user dict.

    Requires Entra ID SSO to be configured (entra.mode = 'enabled').
    If not configured, redirects to a setup hint page.
    """
    # Read entra.mode from DB
    mode_row = await db.execute(
        select(AppConfig).where(AppConfig.key == "entra.mode")
    )
    mode_cfg = mode_row.scalar_one_or_none()
    mode = (mode_cfg.value or "disabled") if mode_cfg else "disabled"

    if mode == "disabled":
        raise HTTPException(
            status_code=503,
            detail="Portal authentication is not configured. An administrator must enable Entra ID SSO in Admin > Settings.",
        )

    user = request.session.get("portal_user")
    if user:
        return user

    # Store the originally requested URL so callback can redirect back
    request.session["login_next"] = str(request.url)
    raise HTTPException(
        status_code=302,
        headers={"Location": "/portal/login"},
    )

_STATUS_COLORS = {
    "pending":      "bg-gray-100 text-gray-700",
    "scheduled":    "bg-indigo-100 text-indigo-700",
    "processing":   "bg-blue-100 text-blue-700",
    "provisioning": "bg-blue-100 text-blue-700",
    "delivered":    "bg-green-100 text-green-700",
    "provisioned":  "bg-green-100 text-green-700",
    "revoking":     "bg-orange-100 text-orange-700",
    "revoked":      "bg-gray-100 text-gray-500",
    "failed":       "bg-red-100 text-red-700",
    "expired":      "bg-orange-100 text-orange-700",
    "cancelled":        "bg-gray-100 text-gray-500",
    "pending_approval": "bg-amber-100 text-amber-700",
    "rejected":         "bg-red-100 text-red-700",
}

_STEP_COLORS = {
    "pending": "bg-gray-100 text-gray-600",
    "running": "bg-blue-100 text-blue-700",
    "success": "bg-green-100 text-green-700",
    "failed":  "bg-red-100 text-red-700",
    "skipped": "bg-gray-100 text-gray-400",
}


# ── Overview ───────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def portal_index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
):
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.steps))
        .where(_user_order_filter(current_user["email"]))
        .order_by(Order.created_at.desc())
        .limit(100)
    )
    orders = list(result.scalars().all())

    type_ids = {o.asset_type_id for o in orders}
    asset_type_names: dict[int, str] = {}
    asset_type_categories: dict[int, str] = {}
    if type_ids:
        types_result = await db.execute(
            select(AssetType).where(AssetType.id.in_(type_ids))
        )
        for t in types_result.scalars().all():
            asset_type_names[t.id] = t.name
            asset_type_categories[t.id] = t.category.value

    # Asset name lookup for personally assigned assets
    asset_ids = [o.assigned_asset_id for o in orders if o.assigned_asset_id]
    asset_names: dict[int, str] = {}
    if asset_ids:
        asset_rows = await db.execute(
            select(AssetPool.id, AssetPool.name).where(AssetPool.id.in_(asset_ids))
        )
        asset_names = {row.id: row.name for row in asset_rows}

    return templates.TemplateResponse("portal/index.html", {
        "request": request,
        "active_page": "overview",
        "user": current_user,
        "orders": orders,
        "asset_type_names": asset_type_names,
        "asset_type_categories": asset_type_categories,
        "asset_names": asset_names,
        "status_colors": _STATUS_COLORS,
    })


# ── Pool availability helper ──────────────────────────────────────────────────

_ACTIVE_ORDER_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.SCHEDULED,
    OrderStatus.PROCESSING,
    OrderStatus.PROVISIONING,
    OrderStatus.PROVISIONED,
    OrderStatus.DELIVERED,
)


async def _get_unavailable_type_ids(db: AsyncSession, asset_types: list) -> set[int]:
    """Return set of asset_type IDs that have no free assets available.

    Checks two things:
    - capacity_pooled types: active orders >= pool_capacity
    - All types with pool assets: 0 free assets in asset_pool
    """
    unavailable: set[int] = set()
    type_ids = [t.id for t in asset_types]
    if not type_ids:
        return unavailable

    # 1) Check actual pool: any type with pool assets but 0 free ones
    free_result = await db.execute(
        select(AssetPool.asset_type_id, sa_func.count())
        .where(
            AssetPool.asset_type_id.in_(type_ids),
            AssetPool.status == AssetStatus.FREE,
        )
        .group_by(AssetPool.asset_type_id)
    )
    free_counts = {row[0]: row[1] for row in free_result.all()}

    # Count total pool assets per type (to distinguish "no pool" from "empty pool")
    total_result = await db.execute(
        select(AssetPool.asset_type_id, sa_func.count())
        .where(AssetPool.asset_type_id.in_(type_ids))
        .group_by(AssetPool.asset_type_id)
    )
    total_counts = {row[0]: row[1] for row in total_result.all()}

    for t in asset_types:
        has_pool = total_counts.get(t.id, 0) > 0
        free = free_counts.get(t.id, 0)
        if has_pool and free == 0:
            unavailable.add(t.id)

    # 2) Additional check for capacity_pooled: active orders >= pool_capacity
    pooled = [t for t in asset_types if t.assignment_model == "capacity_pooled" and t.pool_capacity]
    if pooled:
        pooled_ids = [t.id for t in pooled]
        order_result = await db.execute(
            select(Order.asset_type_id, sa_func.count())
            .where(
                Order.asset_type_id.in_(pooled_ids),
                Order.status.in_(_ACTIVE_ORDER_STATUSES),
            )
            .group_by(Order.asset_type_id)
        )
        order_counts = {row[0]: row[1] for row in order_result.all()}
        for t in pooled:
            if order_counts.get(t.id, 0) >= t.pool_capacity:
                unavailable.add(t.id)

    return unavailable


def _filter_eligible_asset_types(asset_types: list, user_email: str) -> list:
    """Filter asset types to only those the user is eligible to request.

    Asset types with an ``eligible_requestors_dn`` are restricted to members
    of that AD group.  Types without a DN are open to all domain users.
    """
    from app.utils.ad_lookup import check_group_membership

    # Collect unique group DNs to avoid duplicate LDAP calls
    unique_dns: dict[str, bool] = {}
    for t in asset_types:
        dn = getattr(t, "eligible_requestors_dn", None)
        if dn and dn not in unique_dns:
            result = check_group_membership(user_email, dn)
            unique_dns[dn] = result.get("is_member", False) if result.get("success") else True

    eligible = []
    for t in asset_types:
        dn = getattr(t, "eligible_requestors_dn", None)
        if not dn:
            eligible.append(t)
        elif unique_dns.get(dn, True):
            eligible.append(t)
    return eligible


# ── New Order ──────────────────────────────────────────────────────────────────

@router.get("/orders/new", response_class=HTMLResponse)
async def portal_new_order_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
):
    types_result = await db.execute(select(AssetType).order_by(AssetType.name))
    all_types = list(types_result.scalars().all())
    asset_types = _filter_eligible_asset_types(all_types, current_user["email"])
    unavailable_ids = await _get_unavailable_type_ids(db, asset_types)

    # Load max advance days setting
    max_adv_row = await db.execute(
        select(AppConfig.value).where(AppConfig.key == "portal.max_advance_days")
    )
    max_advance_days = int((max_adv_row.scalar_one_or_none() or "0") or "0")
    max_from_date = (date.today() + timedelta(days=max_advance_days)).isoformat() if max_advance_days > 0 else ""

    return templates.TemplateResponse("portal/order_new.html", {
        "request": request,
        "active_page": "new",
        "user": current_user,
        "asset_types": asset_types,
        "unavailable_ids": unavailable_ids,
        "today": date.today().isoformat(),
        "max_advance_days": max_advance_days,
        "max_from_date": max_from_date,
        "error": None,
    })


def _validate_order_attrs(
    form_data,
    attr_defs: list[dict],
) -> tuple[dict | None, str | None]:
    """Validates attr_* fields against AttributeDefinitions from AssetType.config.

    Returns (collected_config, None) on success or (None, error_msg) on error.
    Non-visible fields (visibleWhen not satisfied) are ignored.
    """
    from app.schemas.admin import AttributeDefinition, AttributeType

    if not attr_defs:
        return None, None

    # Convert raw dicts → AttributeDefinition (ignores invalid entries)
    defs: list[AttributeDefinition] = []
    for raw in attr_defs:
        try:
            # Backwards-compat: altes Format ohne `type` → ENUM wenn options vorhanden
            if "type" not in raw:
                raw = dict(raw)
                raw["type"] = "ENUM" if raw.get("options") else "STRING"
            defs.append(AttributeDefinition.model_validate(raw))
        except Exception:
            pass  # Skip invalid definitions

    collected: dict[str, object] = {}
    for attr in defs:
        # Check visibleWhen: if condition not met → skip field
        if attr.visible_when:
            cond_field = attr.visible_when.get("field", "")
            cond_value = attr.visible_when.get("value", "")
            form_val = form_data.get(cond_field, "")
            if str(form_val) != str(cond_value):
                continue  # not visible → skip validation

        raw_val = form_data.getlist("attr_" + attr.key) if attr.type == AttributeType.MULTI_ENUM else form_data.get("attr_" + attr.key, "")

        # Validate required field
        empty = (raw_val == "" or raw_val == [] or raw_val is None)
        if attr.required and empty:
            return None, f"Required field '{attr.label}' was not filled in."

        if empty:
            if attr.default_value is not None:
                collected[attr.key] = attr.default_value
            continue

        # Type conversion and value validation
        if attr.type == AttributeType.INT:
            try:
                collected[attr.key] = int(raw_val)
            except (ValueError, TypeError):
                return None, f"Field '{attr.label}' must be an integer."
        elif attr.type == AttributeType.BOOL:
            collected[attr.key] = raw_val in ("true", "True", "1", "on", True)
        elif attr.type == AttributeType.ENUM:
            if attr.options and str(raw_val) not in attr.options:
                return None, f"Invalid value for '{attr.label}'."
            collected[attr.key] = str(raw_val)
        elif attr.type == AttributeType.MULTI_ENUM:
            vals = raw_val if isinstance(raw_val, list) else [raw_val]
            if attr.options:
                invalid = [v for v in vals if v not in attr.options]
                if invalid:
                    return None, f"Invalid values for '{attr.label}': {', '.join(invalid)}"
            collected[attr.key] = vals
        else:
            collected[attr.key] = str(raw_val)

    return (collected if collected else None), None


@router.post("/orders/new")
async def portal_create_order(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
    user_name: str = Form(...),
    user_email: str = Form(...),
    owner_name: str = Form(""),
    owner_email: str = Form(""),
    asset_type_id: int = Form(...),
    requested_from: str = Form(...),
    requested_until: str = Form(...),
    rdp_users: list[str] = Form(default=[]),
    admin_users: list[str] = Form(default=[]),
):
    # Read all form fields (for attr_* fields)
    form_data = await request.form()

    async def _render_error(msg: str):
        types_result = await db.execute(select(AssetType).order_by(AssetType.name))
        all_types_raw = list(types_result.scalars().all())
        all_types = _filter_eligible_asset_types(all_types_raw, current_user["email"])
        max_adv_row = await db.execute(
            select(AppConfig.value).where(AppConfig.key == "portal.max_advance_days")
        )
        max_adv = int((max_adv_row.scalar_one_or_none() or "0") or "0")
        max_from = (date.today() + timedelta(days=max_adv)).isoformat() if max_adv > 0 else ""
        return templates.TemplateResponse("portal/order_new.html", {
            "request": request,
            "active_page": "new",
            "user": current_user,
            "asset_types": all_types,
            "unavailable_ids": await _get_unavailable_type_ids(db, all_types),
            "today": date.today().isoformat(),
            "max_advance_days": max_adv,
            "max_from_date": max_from,
            "error": msg,
            # Return form values
            "form": {
                "user_name": user_name, "user_email": user_email,
                "owner_name": owner_name, "owner_email": owner_email,
                "asset_type_id": asset_type_id,
                "requested_from": requested_from, "requested_until": requested_until,
                "rdp_users": [u for u in rdp_users if u.strip()],
                "admin_users": [u for u in admin_users if u.strip()],
            },
        }, status_code=422)

    try:
        from_dt = datetime.fromisoformat(requested_from).replace(tzinfo=timezone.utc)
        until_dt = datetime.fromisoformat(requested_until).replace(tzinfo=timezone.utc)
    except ValueError:
        return await _render_error("Invalid date format.")

    if until_dt <= from_dt:
        return await _render_error("The end date must be after the start date.")

    # Validate max advance days
    max_adv_row = await db.execute(
        select(AppConfig.value).where(AppConfig.key == "portal.max_advance_days")
    )
    max_advance_days = int((max_adv_row.scalar_one_or_none() or "0") or "0")
    if max_advance_days > 0:
        max_date = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc) + timedelta(days=max_advance_days)
        if from_dt > max_date:
            return await _render_error(
                f"The start date cannot be more than {max_advance_days} days in the future."
            )

    # Load asset type for attribute validation
    at_result = await db.execute(select(AssetType).where(AssetType.id == asset_type_id))
    asset_type = at_result.scalar_one_or_none()
    if not asset_type:
        return await _render_error("Unknown asset type.")

    # Eligibility check — ensure user is member of the required group
    if asset_type.eligible_requestors_dn:
        from app.utils.ad_lookup import check_group_membership
        membership = check_group_membership(user_email, asset_type.eligible_requestors_dn)
        if membership.get("success") and not membership.get("is_member"):
            return await _render_error("You are not eligible to request this asset type.")

    # Pool availability check
    unavail = await _get_unavailable_type_ids(db, [asset_type])
    if asset_type.id in unavail:
        return await _render_error(
            f"No free assets available for \"{asset_type.name}\". Please try again later."
        )

    order_config, attr_error = _validate_order_attrs(form_data, asset_type.config or [])
    if attr_error:
        return await _render_error(attr_error)

    rdp_clean = [u.strip() for u in rdp_users if u.strip()]
    admin_clean = [u.strip() for u in admin_users if u.strip()]

    # Determine if this is a future-dated order
    is_future = from_dt.date() > date.today()

    # ── Approval gate ────────────────────────────────────────────────────────
    needs_manager_approval = asset_type.requires_manager_approval
    needs_owner_approval = asset_type.requires_owner_approval
    needs_any_approval = needs_manager_approval or needs_owner_approval

    manager_info = None
    if needs_manager_approval:
        from app.utils.ad_lookup import lookup_manager
        mgr_result = lookup_manager(user_email)
        if not mgr_result.get("success"):
            return await _render_error(
                "Could not look up your manager information in Active Directory. Please contact support."
            )
        manager_info = mgr_result.get("manager")
        if manager_info is None:
            return await _render_error(
                "This asset can only be ordered through management approval but there is "
                "currently no manager configured in Active Directory for your account. "
                "Please contact support."
            )

    # Determine initial status
    if needs_any_approval:
        initial_status = OrderStatus.PENDING_APPROVAL
    elif is_future:
        initial_status = OrderStatus.SCHEDULED
    else:
        initial_status = OrderStatus.PENDING

    order = Order(
        user_email=user_email,
        user_name=user_name,
        owner_email=owner_email or None,
        owner_name=owner_name or None,
        asset_type_id=asset_type_id,
        rdp_users=rdp_clean,
        admin_users=admin_clean,
        requested_from=from_dt,
        requested_until=until_dt,
        action=OrderAction.PROVISION,
        status=initial_status,
        config=order_config,
    )
    db.add(order)
    await db.flush()

    if needs_any_approval:
        # Create approval records
        from app.models.approval import OrderApproval

        if needs_manager_approval and manager_info:
            db.add(OrderApproval(
                order_id=order.id,
                approver_type="manager",
                approver_email=manager_info["email"],
                approver_name=manager_info["display_name"],
            ))

        if needs_owner_approval and asset_type.approval_owners:
            for owner in asset_type.approval_owners:
                db.add(OrderApproval(
                    order_id=order.id,
                    approver_type="application_owner",
                    approver_email=owner["email"],
                    approver_name=owner.get("name", owner["email"]),
                ))

        await db.flush()

        # Send approval request emails via Celery
        from celery import Celery
        celery_app = Celery(broker=settings.CELERY_BROKER_URL)
        celery_app.send_task(
            "tasks.workflows.dynamic_runner.send_approval_requests",
            args=[order.id],
            queue="provision",
        )
        logger.info("Portal: Order %s created with pending approval, user=%s", order.id, order.user_email)

    elif is_future:
        # Future-dated: reserve asset now, dispatch runbook later on start date
        needs_asset = asset_type.assignment_model in ("assigned_personal", "dedicated_shared")
        if needs_asset:
            # Reserve a free asset immediately so it's guaranteed on start day
            reserve_row = await db.execute(sql_text("""
                SELECT id, name FROM asset_pool
                WHERE asset_type_id = :at AND status = 'free'
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """), {"at": asset_type_id})
            free_asset = reserve_row.fetchone()
            if not free_asset:
                return await _render_error(
                    f"No free assets available for \"{asset_type.name}\". Please try again later."
                )
            await db.execute(sql_text("""
                UPDATE asset_pool
                SET status = 'reserved', current_order_id = :oid, expires_at = :exp
                WHERE id = :aid
            """), {"aid": free_asset.id, "oid": order.id, "exp": until_dt})
            order.assigned_asset_id = free_asset.id
            logger.info(
                "Portal: Reserved asset %s (%s) for scheduled order %s",
                free_asset.id, free_asset.name, order.id,
            )

        # Send confirmation email immediately so user knows the order is received
        from celery import Celery
        celery_app = Celery(broker=settings.CELERY_BROKER_URL)
        celery_app.send_task(
            "tasks.workflows.dynamic_runner.send_scheduled_confirmation",
            args=[order.id],
            queue="provision",
        )
        logger.info(
            "Portal: Scheduled order created id=%s user=%s start=%s",
            order.id, order.user_email, from_dt.date().isoformat(),
        )
    else:
        # Immediate: dispatch runbook now
        from app.routes.webhook import _dispatch_runbook
        task_id = _dispatch_runbook(order)
        order.celery_task_id = task_id
        order.status = OrderStatus.PROCESSING
        logger.info("Portal: Order created id=%s user=%s", order.id, order.user_email)

    await db.commit()
    return RedirectResponse(url=f"/portal/orders/{order.id}", status_code=303)


# ── Order Detail ───────────────────────────────────────────────────────────────

@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def portal_order_detail(
    request: Request,
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
):
    result = await db.execute(
        select(Order).options(selectinload(Order.steps)).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    _assert_owns_order(order, current_user["email"])

    asset_type_name = None
    asset_type = None
    if order.asset_type_id:
        asset_type = await db.get(AssetType, order.asset_type_id)
        asset_type_name = asset_type.name if asset_type else None

    asset_name = None
    if order.assigned_asset_id and asset_type and asset_type.category.value == "platform_access":
        asset_row = await db.execute(
            select(AssetPool.name).where(AssetPool.id == order.assigned_asset_id)
        )
        asset_name = asset_row.scalar_one_or_none()

    steps_with_duration = []
    for step in sorted(order.steps, key=lambda s: s.id):
        duration = None
        if step.started_at and step.finished_at:
            secs = (step.finished_at - step.started_at).total_seconds()
            duration = f"{secs:.1f}s"
        steps_with_duration.append({"step": step, "duration": duration})

    # Load approval records (if any)
    approvals = []
    if order.status in (OrderStatus.PENDING_APPROVAL, OrderStatus.REJECTED):
        from app.models.approval import OrderApproval
        appr_result = await db.execute(
            select(OrderApproval).where(OrderApproval.order_id == order.id)
        )
        approvals = list(appr_result.scalars().all())

    return templates.TemplateResponse("portal/order_detail.html", {
        "request": request,
        "active_page": "overview",
        "user": current_user,
        "order": order,
        "asset_type": asset_type,
        "asset_type_name": asset_type_name,
        "asset_name": asset_name,
        "steps_with_duration": steps_with_duration,
        "approvals": approvals,
        "status_colors": _STATUS_COLORS,
        "step_colors": _STEP_COLORS,
        "today": date.today().isoformat(),
    })


# ── Extend ─────────────────────────────────────────────────────────────────────

@router.post("/orders/{order_id}/extend")
async def portal_extend_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
    new_until: str = Form(...),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Order not found")
    _assert_owns_order(original, current_user["email"])

    try:
        until_dt = datetime.fromisoformat(new_until).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    new_order = Order(
        user_email=original.user_email,
        user_name=original.user_name,
        owner_email=original.owner_email,
        owner_name=original.owner_name,
        asset_type_id=original.asset_type_id,
        assigned_asset_id=original.assigned_asset_id,
        rdp_users=original.rdp_users,
        admin_users=original.admin_users,
        requested_from=original.requested_from,
        requested_until=until_dt,
        action=OrderAction.EXTEND,
        status=OrderStatus.PENDING,
    )
    db.add(new_order)
    await db.flush()

    from app.routes.webhook import _dispatch_runbook
    new_order.celery_task_id = _dispatch_runbook(new_order)
    new_order.status = OrderStatus.PROCESSING
    await db.commit()

    logger.info("Portal: Extend order id=%s from order=%s", new_order.id, order_id)
    return RedirectResponse(url=f"/portal/orders/{new_order.id}", status_code=303)


# ── Modify Access ──────────────────────────────────────────────────────────────

@router.post("/orders/{order_id}/modify")
async def portal_modify_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
    rdp_users: list[str] = Form(default=[]),
    admin_users: list[str] = Form(default=[]),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Order not found")
    _assert_owns_order(original, current_user["email"])

    rdp_clean = [u.strip() for u in rdp_users if u.strip()]
    admin_clean = [u.strip() for u in admin_users if u.strip()]

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
        requested_until=original.requested_until,
        action=OrderAction.MODIFY,
        status=OrderStatus.PENDING,
    )
    db.add(new_order)
    await db.flush()

    from app.routes.webhook import _dispatch_runbook
    new_order.celery_task_id = _dispatch_runbook(new_order)
    new_order.status = OrderStatus.PROCESSING
    await db.commit()

    logger.info("Portal: Modify order id=%s from order=%s", new_order.id, order_id)
    return RedirectResponse(url=f"/portal/orders/{new_order.id}", status_code=303)


# ── Modify Asset (combined: duration + user lists) ─────────────────────────────

@router.post("/orders/{order_id}/change")
async def portal_change_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
    new_until: str | None = Form(default=None),
    rdp_users: list[str] = Form(default=[]),
    admin_users: list[str] = Form(default=[]),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Order not found")
    _assert_owns_order(original, current_user["email"])

    is_active = original.status in (OrderStatus.DELIVERED, OrderStatus.PROVISIONED)
    is_failed_change = (
        original.status == OrderStatus.FAILED
        and original.action in (OrderAction.MODIFY, OrderAction.EXTEND)
    )
    if not (is_active or is_failed_change):
        raise HTTPException(
            status_code=422,
            detail="Only active orders (DELIVERED/PROVISIONED) can be modified",
        )

    # Resolve requested_until
    if new_until:
        try:
            requested_until = datetime.fromisoformat(new_until).replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
    else:
        requested_until = original.requested_until

    rdp_clean = [u.strip() for u in rdp_users if u.strip()]
    admin_clean = [u.strip() for u in admin_users if u.strip()]

    # Sync asset expires_at when the date changed
    if original.assigned_asset_id and requested_until != original.requested_until:
        from app.models.asset import AssetPool
        asset = await db.get(AssetPool, original.assigned_asset_id)
        if asset:
            asset.expires_at = requested_until

    # ── Check if re-approval is needed on user-list changes ────────────────────
    asset_type = await db.get(AssetType, original.asset_type_id)
    users_changed = (
        sorted(rdp_clean) != sorted(original.rdp_users or [])
        or sorted(admin_clean) != sorted(original.admin_users or [])
    )
    needs_reapproval = (
        asset_type
        and asset_type.requires_approval_on_modify
        and users_changed
        and (asset_type.requires_manager_approval or asset_type.requires_owner_approval)
    )

    initial_status = OrderStatus.PENDING_APPROVAL if needs_reapproval else OrderStatus.PENDING

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
        status=initial_status,
    )
    db.add(new_order)
    await db.flush()

    if needs_reapproval:
        # Create approval records (same logic as new-order approval gate)
        from app.models.approval import OrderApproval

        if asset_type.requires_manager_approval:
            from app.utils.ad_lookup import lookup_manager
            mgr_result = lookup_manager(original.user_email)
            manager_info = mgr_result.get("manager") if mgr_result.get("success") else None
            if manager_info:
                db.add(OrderApproval(
                    order_id=new_order.id,
                    approver_type="manager",
                    approver_email=manager_info["email"],
                    approver_name=manager_info["display_name"],
                ))

        if asset_type.requires_owner_approval and asset_type.approval_owners:
            for owner in asset_type.approval_owners:
                db.add(OrderApproval(
                    order_id=new_order.id,
                    approver_type="application_owner",
                    approver_email=owner["email"],
                    approver_name=owner.get("name", owner["email"]),
                ))

        await db.flush()

        # Send approval request emails
        from celery import Celery
        celery_app = Celery(broker=settings.CELERY_BROKER_URL)
        celery_app.send_task(
            "tasks.workflows.dynamic_runner.send_approval_requests",
            args=[new_order.id],
            queue="provision",
        )
        logger.info("Portal: Modify order %s requires re-approval, user=%s", new_order.id, original.user_email)
    else:
        from app.routes.webhook import _dispatch_runbook
        new_order.celery_task_id = _dispatch_runbook(new_order)
        new_order.status = OrderStatus.PROCESSING

    await db.commit()

    logger.info("Portal: Change order id=%s from order=%s", new_order.id, order_id)
    return RedirectResponse(url=f"/portal/orders/{new_order.id}", status_code=303)


# ── Cancel ─────────────────────────────────────────────────────────────────────

@router.post("/orders/{order_id}/cancel")
async def portal_cancel_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Order not found")
    _assert_owns_order(original, current_user["email"])

    if original.status not in (OrderStatus.DELIVERED, OrderStatus.PROVISIONED, OrderStatus.SCHEDULED):
        raise HTTPException(
            status_code=422,
            detail="Only active or scheduled orders can be cancelled",
        )

    # Scheduled orders: cancel + release reserved asset (nothing provisioned yet)
    if original.status == OrderStatus.SCHEDULED:
        if original.assigned_asset_id:
            await db.execute(sql_text("""
                UPDATE asset_pool
                SET status = 'free', current_order_id = NULL, expires_at = NULL
                WHERE id = :aid AND status = 'reserved'
            """), {"aid": original.assigned_asset_id})
            logger.info("Portal: Released reserved asset %s for cancelled order %s",
                        original.assigned_asset_id, order_id)
        original.status = OrderStatus.CANCELLED
        await db.commit()
        logger.info("Portal: Scheduled order cancelled id=%s", order_id)
        return RedirectResponse(url=f"/portal/orders/{order_id}", status_code=303)

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
        # Copy snapshot from provision order → deterministic revoke
        provisioned_state=original.provisioned_state,
    )
    db.add(cancel_order)
    await db.flush()

    from app.routes.webhook import _dispatch_runbook
    cancel_order.celery_task_id = _dispatch_runbook(cancel_order)
    cancel_order.status = OrderStatus.PROCESSING

    # Mark original provision order as cancelled so it disappears from My IT
    original.status = OrderStatus.CANCELLED
    await db.commit()

    logger.info("Portal: Cancel order id=%s from order=%s", cancel_order.id, order_id)
    return RedirectResponse(url=f"/portal/orders/{cancel_order.id}", status_code=303)


# ── My IT – Active Assets Overview ────────────────────────────────────────────

@router.get("/my-it", response_class=HTMLResponse)
async def portal_my_it(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
):


    result = await db.execute(
        select(Order)
        .options(selectinload(Order.steps))
        .where(_user_order_filter(current_user["email"]))
        .where(Order.action == OrderAction.PROVISION)
        .where(Order.status.in_([OrderStatus.DELIVERED, OrderStatus.PROVISIONED, OrderStatus.SCHEDULED]))
        .order_by(Order.created_at.desc())
    )
    raw_orders = list(result.scalars().all())

    type_ids = {o.asset_type_id for o in raw_orders}
    asset_type_names: dict[int, str] = {}
    asset_type_categories: dict[int, str] = {}
    asset_types_by_id: dict[int, AssetType] = {}
    if type_ids:
        types_result = await db.execute(select(AssetType).where(AssetType.id.in_(type_ids)))
        for t in types_result.scalars().all():
            asset_type_names[t.id] = t.name
            asset_type_categories[t.id] = t.category.value
            asset_types_by_id[t.id] = t

    asset_ids = [o.assigned_asset_id for o in raw_orders if o.assigned_asset_id]
    asset_names: dict[int, str] = {}
    if asset_ids:
        asset_rows = await db.execute(
            select(AssetPool.id, AssetPool.name).where(AssetPool.id.in_(asset_ids))
        )
        asset_names = {row.id: row.name for row in asset_rows}

    # Drop orders whose asset was deleted (assigned_personal requires a live asset)
    # Scheduled orders are exempt: no asset is assigned yet (that happens at dispatch time)
    orders = [
        o for o in raw_orders
        if o.status == OrderStatus.SCHEDULED or not (
            asset_types_by_id.get(o.asset_type_id) is not None
            and asset_types_by_id[o.asset_type_id].assignment_model == "assigned_personal"
            and o.assigned_asset_id is None
        )
    ]

    return templates.TemplateResponse("portal/my_it.html", {
        "request": request,
        "active_page": "my-it",
        "user": current_user,
        "orders": orders,
        "asset_type_names": asset_type_names,
        "asset_type_categories": asset_type_categories,
        "asset_types_by_id": asset_types_by_id,
        "asset_names": asset_names,
        "status_colors": _STATUS_COLORS,
    })


@router.get("/my-it/{order_id}", response_class=HTMLResponse)
async def portal_my_it_detail(
    request: Request,
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    _assert_owns_order(order, current_user["email"])

    if order.status not in (OrderStatus.DELIVERED, OrderStatus.PROVISIONED, OrderStatus.SCHEDULED):
        raise HTTPException(status_code=422, detail="Only active or scheduled assets can be managed here")

    asset_type = None
    asset_type_name = None
    if order.asset_type_id:
        asset_type = await db.get(AssetType, order.asset_type_id)
        asset_type_name = asset_type.name if asset_type else None

    asset_name = None
    if order.assigned_asset_id:
        asset_row = await db.execute(
            select(AssetPool.name).where(AssetPool.id == order.assigned_asset_id)
        )
        asset_name = asset_row.scalar_one_or_none()

    # Determine effective current user lists: use the most recent completed MODIFY
    # order (if any), otherwise fall back to the provision order's lists.
    effective_rdp_users = list(order.rdp_users or [])
    effective_admin_users = list(order.admin_users or [])
    if order.asset_type_id and order.assigned_asset_id:
        latest_modify_result = await db.execute(
            select(Order)
            .where(
                Order.assigned_asset_id == order.assigned_asset_id,
                Order.action == OrderAction.MODIFY,
                Order.status.in_([OrderStatus.DELIVERED, OrderStatus.PROVISIONED]),
            )
            .order_by(Order.id.desc())
            .limit(1)
        )
        latest_modify = latest_modify_result.scalar_one_or_none()
        if latest_modify:
            effective_rdp_users = list(latest_modify.rdp_users or [])
            effective_admin_users = list(latest_modify.admin_users or [])

    return templates.TemplateResponse("portal/my_it_detail.html", {
        "request": request,
        "active_page": "my-it",
        "user": current_user,
        "order": order,
        "asset_type": asset_type,
        "asset_type_name": asset_type_name,
        "asset_name": asset_name,
        "today": date.today().isoformat(),
        "status_colors": _STATUS_COLORS,
        "effective_rdp_users": effective_rdp_users,
        "effective_admin_users": effective_admin_users,
    })


# ── Approvals ─────────────────────────────────────────────────────────────────

@router.get("/approvals", response_class=HTMLResponse)
async def portal_approvals(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
    show_recent: bool = False,
):
    from app.models.approval import OrderApproval

    # Pending approvals for the current user
    pending_result = await db.execute(
        select(OrderApproval)
        .where(OrderApproval.approver_email == current_user["email"])
        .where(OrderApproval.status == "pending")
        .order_by(OrderApproval.created_at.desc())
    )
    pending_rows = list(pending_result.scalars().all())

    # Load associated orders and asset type names
    pending_approvals = []
    for a in pending_rows:
        order = await db.get(Order, a.order_id)
        if not order:
            continue
        at = await db.get(AssetType, order.asset_type_id)
        pending_approvals.append({
            "approval": a,
            "order": order,
            "asset_type_name": at.name if at else f"Type {order.asset_type_id}",
        })

    # Recent decisions (optional)
    recent_approvals = []
    if show_recent:
        recent_result = await db.execute(
            select(OrderApproval)
            .where(OrderApproval.approver_email == current_user["email"])
            .where(OrderApproval.status.in_(["approved", "declined"]))
            .order_by(OrderApproval.decided_at.desc())
            .limit(20)
        )
        for a in recent_result.scalars().all():
            order = await db.get(Order, a.order_id)
            if not order:
                continue
            at = await db.get(AssetType, order.asset_type_id)
            recent_approvals.append({
                "approval": a,
                "order": order,
                "asset_type_name": at.name if at else f"Type {order.asset_type_id}",
            })

    return templates.TemplateResponse("portal/approvals.html", {
        "request": request,
        "active_page": "approvals",
        "user": current_user,
        "pending_approvals": pending_approvals,
        "pending_count": len(pending_approvals),
        "recent_approvals": recent_approvals,
        "show_recent": show_recent,
    })


@router.post("/approvals/{approval_id}/decide")
async def portal_decide_approval(
    request: Request,
    approval_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_portal_auth),
    decision: str = Form(...),
    comment: str = Form(default=""),
):
    from app.models.approval import OrderApproval

    result = await db.execute(select(OrderApproval).where(OrderApproval.id == approval_id))
    approval = result.scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")

    if approval.approver_email != current_user["email"]:
        raise HTTPException(status_code=403, detail="You are not the designated approver")

    if approval.status != "pending":
        return RedirectResponse(url="/portal/approvals", status_code=303)

    # Record decision
    approval.status = "approved" if decision == "approve" else "declined"
    approval.decided_at = datetime.now(timezone.utc)
    approval.comment = comment.strip() or None

    order = await db.get(Order, approval.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    from celery import Celery
    celery_app = Celery(broker=settings.CELERY_BROKER_URL)

    if decision != "approve":
        # Decline: reject the order immediately
        order.status = OrderStatus.REJECTED
        order.error_message = f"Declined by {approval.approver_name}: {comment.strip() or 'no reason given'}"
        celery_app.send_task(
            "tasks.workflows.dynamic_runner.send_approval_result_email",
            args=[order.id, False, approval.approver_name, comment.strip() or None],
            queue="provision",
        )
        logger.info("Approval %s declined by %s for order %s", approval_id, current_user["email"], order.id)
    else:
        # Approve: check if all approvals are now granted
        all_result = await db.execute(
            select(OrderApproval).where(OrderApproval.order_id == order.id)
        )
        all_approvals = list(all_result.scalars().all())
        all_approved = all(a.status == "approved" for a in all_approvals)

        if all_approved:
            # All approved — proceed with order
            await _post_approval_dispatch(order, db, celery_app)
            celery_app.send_task(
                "tasks.workflows.dynamic_runner.send_approval_result_email",
                args=[order.id, True],
                queue="provision",
            )
            logger.info("All approvals granted for order %s — dispatching", order.id)
        else:
            logger.info("Approval %s approved by %s for order %s (still pending others)",
                        approval_id, current_user["email"], order.id)

    await db.commit()
    return RedirectResponse(url="/portal/approvals", status_code=303)


async def _post_approval_dispatch(order: Order, db: AsyncSession, celery_app) -> None:
    """After all approvals granted, transition order to normal flow."""
    asset_type = await db.get(AssetType, order.asset_type_id)
    is_future = order.requested_from and order.requested_from.date() > date.today()

    if is_future:
        order.status = OrderStatus.SCHEDULED
        # Reserve asset for future-dated orders
        if asset_type and asset_type.assignment_model in ("assigned_personal", "dedicated_shared"):
            reserve_row = await db.execute(sql_text("""
                SELECT id, name FROM asset_pool
                WHERE asset_type_id = :at AND status = 'free'
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """), {"at": order.asset_type_id})
            free_asset = reserve_row.fetchone()
            if free_asset:
                await db.execute(sql_text("""
                    UPDATE asset_pool
                    SET status = 'reserved', current_order_id = :oid, expires_at = :exp
                    WHERE id = :aid
                """), {"aid": free_asset.id, "oid": order.id, "exp": order.requested_until})
                order.assigned_asset_id = free_asset.id

        celery_app.send_task(
            "tasks.workflows.dynamic_runner.send_scheduled_confirmation",
            args=[order.id],
            queue="provision",
        )
    else:
        order.status = OrderStatus.PENDING
        from app.routes.webhook import _dispatch_runbook
        task_id = _dispatch_runbook(order)
        order.celery_task_id = task_id
        order.status = OrderStatus.PROCESSING


# ── HTMX: User Validation ──────────────────────────────────────────────────────

@router.get("/_validate-user", response_class=HTMLResponse)
async def portal_validate_user(request: Request, q: str = ""):
    if not q.strip():
        return HTMLResponse("")
    result = lookup_user(q.strip())
    return templates.TemplateResponse("portal/fragments/user_badge.html", {
        "request": request,
        "result": result,
        "identifier": q.strip(),
    })
