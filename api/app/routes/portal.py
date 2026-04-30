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
from app.models.asset import AssetPool, AssetStatus, AssetType, AssignmentModel
from app.models.config import AppConfig
from app.models.order import Order, OrderAction, OrderStatus
from app.utils.ad_lookup import lookup_user, snapshot_requester_attrs
from app.utils.audit import (
    _order_snap, aaudit, classify_for_asset_type_id, portal_actor_by,
)
from app.utils.license import is_feature_enabled

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
    return Response(content=raw, media_type=mime, headers={"Cache-Control": "no-cache"})


@router.get("/asset-type-logo/{type_id}", include_in_schema=False)
async def asset_type_logo(type_id: int, db: AsyncSession = Depends(get_db)) -> Response:
    """Serves an asset type logo image from the DB. Returns 404 when none is set."""
    result = await db.execute(select(AssetType.logo).where(AssetType.id == type_id))
    data_url = result.scalar_one_or_none()
    if not data_url:
        raise HTTPException(status_code=404, detail="No logo configured")
    try:
        header, b64_data = data_url.split(",", 1)
        mime = header.split(":")[1].split(";")[0]
        raw = base64.b64decode(b64_data)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid logo data")
    return Response(content=raw, media_type=mime, headers={"Cache-Control": "no-cache"})


def _user_order_filter(email: str):
    """Returns a SQLAlchemy filter that matches orders belonging to the given user
    (either as requester or as the asset owner)."""
    return or_(Order.user_email == email, Order.owner_email == email)


def _assert_owns_order(order: Order, email: str) -> None:
    """Raises HTTP 403 if the current user is not the requester or owner of the order."""
    if order.user_email != email and order.owner_email != email:
        raise HTTPException(status_code=403, detail="Access denied")


ANONYMOUS_PORTAL_USER = {
    "email": "portal@local",
    "name": "Portal User",
    "oid": "anonymous",
    "upn": "portal@local",
}


async def require_portal_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """FastAPI dependency: returns the authenticated portal user dict.

    Behavior by entra.mode:
    - 'disabled'          → portal open, anonymous shared identity (no login)
    - 'entra_only'        → Entra ID login required
    - 'entra_with_onprem' → Entra ID login + on-prem LDAP check
    """
    mode_row = await db.execute(
        select(AppConfig).where(AppConfig.key == "entra.mode")
    )
    mode_cfg = mode_row.scalar_one_or_none()
    mode = (mode_cfg.value or "disabled") if mode_cfg else "disabled"

    if mode == "disabled":
        return ANONYMOUS_PORTAL_USER

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
    "pending":      "bg-gray-100 text-gray-700 dark:bg-slate-700 dark:text-slate-200",
    "scheduled":    "bg-indigo-100 text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300",
    "processing":   "bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300",
    "provisioning": "bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300",
    "delivered":    "bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300",
    "provisioned":  "bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300",
    "revoking":     "bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-300",
    "revoked":      "bg-gray-100 text-gray-500 dark:bg-slate-700 dark:text-slate-400",
    "failed":       "bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300",
    "expired":      "bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-300",
    "cancelled":        "bg-gray-100 text-gray-500 dark:bg-slate-700 dark:text-slate-400",
    "pending_approval": "bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300",
    "rejected":         "bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300",
}

_STEP_COLORS = {
    "pending": "bg-gray-100 text-gray-600 dark:bg-slate-700 dark:text-slate-300",
    "running": "bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300",
    "success": "bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-300",
    "failed":  "bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-300",
    "skipped": "bg-gray-100 text-gray-400 dark:bg-slate-700 dark:text-slate-500",
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
    # Inactive (deprecated) types stay in the DB for historical orders but
    # don't appear in the catalog.
    types_result = await db.execute(
        select(AssetType).where(AssetType.is_active.is_(True)).order_by(AssetType.name)
    )
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
        types_result = await db.execute(
            select(AssetType).where(AssetType.is_active.is_(True)).order_by(AssetType.name)
        )
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

    # Per-user quota — applies to personal + pooled, not shared instances
    if asset_type.assignment_model != AssignmentModel.DEDICATED_SHARED:
        from app.utils.capacity import enforce_max_per_user
        try:
            await enforce_max_per_user(
                db, asset_type.id, user_email, asset_type.max_per_user
            )
        except HTTPException as exc:
            return await _render_error(exc.detail)

    order_config, attr_error = _validate_order_attrs(form_data, asset_type.config or [])
    if attr_error:
        return await _render_error(attr_error)

    rdp_clean = [u.strip() for u in rdp_users if u.strip()]
    admin_clean = [u.strip() for u in admin_users if u.strip()]

    # Determine if this is a future-dated order
    is_future = from_dt.date() > date.today()

    # Enterprise gates — deputy ordering and scheduled (future-dated) orders
    is_deputy = bool(owner_email.strip() and owner_email.strip().lower() != user_email.strip().lower())
    if is_deputy and not is_feature_enabled("deputy_support"):
        return await _render_error(
            "Ordering on behalf of another user requires an ip·Solis Enterprise license."
        )
    if is_future and not is_feature_enabled("scheduled_orders"):
        return await _render_error(
            "Scheduled (future-dated) orders require an ip·Solis Enterprise license."
        )

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

    # Chargeback snapshot — same helper used by the /orders API and the
    # ServiceNow webhook so all three creation paths produce the same
    # requester_* columns.
    requester_attrs = snapshot_requester_attrs(user_email)

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
        **requester_attrs,
    )
    db.add(order)
    await db.flush()

    # Conditional approval rules — may add approvers regardless of the
    # static manager / owner toggles. We evaluate after the order row
    # exists so dates and requester attrs are available in the context.
    from app.utils.approval_rules import build_context, evaluate_rules
    rule_approvers = evaluate_rules(
        asset_type.approval_rules,
        build_context(order, asset_type),
    )

    # Per-classification approval routing — defaults-driven path that
    # injects an extra approval step when the asset type's attribute
    # classification (PII / PHI / PCI) matches a configured policy.
    # Two policy modes per class:
    #   - compliance_officer → one step at the global officer
    #   - owner_of_record    → one step per asset_type.approval_owners
    # Runs alongside the rules engine, de-duped against existing
    # approvers (manager / app-owner / rule).
    from app.utils.classification_routing import (
        classification_approvers,
        load_classification_policy,
    )
    classification_policy = await load_classification_policy(db)
    classification_extra = classification_approvers(asset_type, classification_policy)

    if needs_any_approval or rule_approvers or classification_extra:
        # Create approval records, applying delegation re-routing if active
        from app.models.approval import OrderApproval
        from app.utils.approval_delegation import resolve_active_delegate

        async def _make_approval(
            approver_type: str,
            email: str,
            name: str,
            *,
            rule_name: str | None = None,
            rule_threshold: int | None = None,
            sod_exempt: bool = False,
        ) -> OrderApproval:
            d = await resolve_active_delegate(db, email)
            if d is None:
                return OrderApproval(
                    order_id=order.id, approver_type=approver_type,
                    approver_email=email, approver_name=name,
                    rule_name=rule_name, rule_threshold=rule_threshold,
                    sod_exempt=sod_exempt,
                )
            # Active delegation — route to the deputy. The original
            # assignee is captured in the audit trail via the
            # delegation row itself; the approval comment is left
            # blank so the deputy's decision text isn't overwritten.
            logger.info(
                "Portal: order %s approval re-routed: %s → %s (delegation %s, until %s)",
                order.id, email, d.delegate_email, d.id, d.until_at.isoformat(),
            )
            return OrderApproval(
                order_id=order.id, approver_type=approver_type,
                approver_email=d.delegate_email,
                approver_name=d.delegate_name or d.delegate_email,
                rule_name=rule_name, rule_threshold=rule_threshold,
                sod_exempt=sod_exempt,
            )

        # Track which emails are already covered so the rule loop
        # below doesn't add duplicates of manager / owner approvers.
        seen_emails: set[str] = set()

        if needs_manager_approval and manager_info:
            db.add(await _make_approval(
                "manager",
                manager_info["email"],
                manager_info["display_name"],
            ))
            seen_emails.add(manager_info["email"].lower())

        if needs_owner_approval and asset_type.approval_owners:
            for owner in asset_type.approval_owners:
                db.add(await _make_approval(
                    "application_owner",
                    owner["email"],
                    owner.get("name", owner["email"]),
                ))
                seen_emails.add(owner["email"].lower())

        for ra in rule_approvers:
            if ra["email"].lower() in seen_emails:
                continue  # already covered by manager / owner — don't double-charge
            db.add(await _make_approval(
                "rule:" + ra["rule_name"][:24],  # approver_type column is String(30)
                ra["email"],
                ra["name"],
                rule_name=ra["rule_name"],          # full, untruncated for grouping
                rule_threshold=ra.get("rule_threshold"),
                sod_exempt=ra.get("sod_exempt", False),
            ))
            seen_emails.add(ra["email"].lower())

        # Per-classification step(s). Skips entries already covered by
        # manager / owner / rule approvers — common case is "the
        # manager and the compliance officer are the same person on a
        # small team" or "the owner-of-record was already added via
        # the static requires_owner_approval flag". We don't want to
        # double-charge any of those people.
        # ``approver_type`` is "compliance_officer" or "owner_of_record"
        # so the audit log distinguishes classification-policy-driven
        # rows from the static manager / application_owner rows.
        for ca in classification_extra:
            if ca["email"].lower() in seen_emails:
                logger.info(
                    "Portal: order %s skips classification %s step for %s "
                    "(already an approver via manager/owner/rule)",
                    order.id, ca["policy"], ca["email"],
                )
                continue
            approver_type = ca["policy"]  # 'compliance_officer' or 'owner_of_record'
            db.add(await _make_approval(
                approver_type,
                ca["email"],
                ca["name"],
            ))
            seen_emails.add(ca["email"].lower())
            logger.info(
                "Portal: order %s adds %s step (trigger=%s, recipient=%s)",
                order.id, ca["policy"], ca["trigger_class"], ca["email"],
            )

        await db.flush()

        # Make sure the order ends up in pending_approval if rules or
        # the classification-routing path added approvers even though
        # the static toggles were off.
        if not needs_any_approval and (rule_approvers or classification_extra):
            order.status = OrderStatus.PENDING_APPROVAL

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
                WHERE asset_type_id = :at AND status = 'Free'
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

    # Audit row written after the routing branch so the snapshot captures
    # the final status (PENDING_APPROVAL / SCHEDULED / PROCESSING) and the
    # asset reservation when applicable. Classification is inherited from
    # the asset type so PII-bearing orders fall under the matching
    # retention window.
    await aaudit(
        db, "order", order.id, "created",
        new=_order_snap(order),
        by=portal_actor_by(current_user, "portal_create_order"),
        classification=await classify_for_asset_type_id(db, order.asset_type_id),
    )

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
            start = step.started_at
            if step.finished_at.tzinfo and not start.tzinfo:
                start = start.replace(tzinfo=step.finished_at.tzinfo)
            # Clamp clock-skew negatives so we don't render "-0.0s".
            secs = max(0.0, (step.finished_at - start).total_seconds())
            duration = "< 1s" if secs < 0.05 else f"{secs:.1f}s"
        steps_with_duration.append({"step": step, "duration": duration})

    # Load approval records (if any)
    approvals = []
    if order.status in (OrderStatus.PENDING_APPROVAL, OrderStatus.REJECTED):
        from app.models.approval import OrderApproval
        appr_result = await db.execute(
            select(OrderApproval).where(OrderApproval.order_id == order.id)
        )
        approvals = list(appr_result.scalars().all())

    cost_projection = _compute_cost_projection(order, asset_type)

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
        "cost_projection": cost_projection,
        "status_colors": _STATUS_COLORS,
        "step_colors": _STEP_COLORS,
        "today": date.today().isoformat(),
    })


# Average month length used to convert a request window in days to a
# fractional cost over its monthly_cost. 30.4375 = 365.25 / 12 — close
# enough for chargeback projections; the cost report's CSV uses the same
# unit when finance pivots the per-order data.
_AVG_DAYS_PER_MONTH = 30.4375


def _compute_cost_projection(order: Order, asset_type: AssetType | None) -> dict | None:
    """Estimate ``monthly_cost × months_requested`` for the order detail card.

    Returns ``None`` when projection is meaningless — no priced asset
    type, no requested-from/until window, or zero-day window. Operators
    set ``monthly_cost`` (+ optional ``currency``) on the asset type;
    untracked types render no cost block on the portal.
    """
    if asset_type is None or asset_type.monthly_cost is None:
        return None
    if not order.requested_from or not order.requested_until:
        return None
    span_days = max(0, (order.requested_until.date() - order.requested_from.date()).days)
    if span_days == 0:
        return None
    unit = float(asset_type.monthly_cost)
    months = span_days / _AVG_DAYS_PER_MONTH
    return {
        "unit_monthly_cost": round(unit, 2),
        "currency": asset_type.currency or "EUR",
        "span_days": span_days,
        "months_estimate": round(months, 2),
        "projected_total": round(unit * months, 2),
    }


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
        and original.action == OrderAction.MODIFY
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
        # Inherit the AD snapshot — modifying an order shouldn't refetch
        # AD; the requester's department at order time is the chargeback
        # truth even if they've moved teams since.
        requester_sam_account=original.requester_sam_account,
        requester_department=original.requester_department,
        requester_cost_center=original.requester_cost_center,
        requester_company=original.requester_company,
        requester_employee_id=original.requester_employee_id,
        requester_title=original.requester_title,
    )
    db.add(new_order)
    await db.flush()

    if needs_reapproval:
        # Create approval records (same logic as new-order approval gate),
        # applying delegation re-routing where applicable.
        from app.models.approval import OrderApproval
        from app.utils.approval_delegation import resolve_active_delegate

        async def _make_reapproval(approver_type: str, email: str, name: str) -> OrderApproval:
            d = await resolve_active_delegate(db, email)
            if d is None:
                return OrderApproval(
                    order_id=new_order.id, approver_type=approver_type,
                    approver_email=email, approver_name=name,
                )
            logger.info(
                "Portal: re-approval for order %s re-routed: %s → %s (delegation %s)",
                new_order.id, email, d.delegate_email, d.id,
            )
            return OrderApproval(
                order_id=new_order.id, approver_type=approver_type,
                approver_email=d.delegate_email,
                approver_name=d.delegate_name or d.delegate_email,
            )

        if asset_type.requires_manager_approval:
            from app.utils.ad_lookup import lookup_manager
            mgr_result = lookup_manager(original.user_email)
            manager_info = mgr_result.get("manager") if mgr_result.get("success") else None
            if manager_info:
                db.add(await _make_reapproval(
                    "manager", manager_info["email"], manager_info["display_name"],
                ))

        if asset_type.requires_owner_approval and asset_type.approval_owners:
            for owner in asset_type.approval_owners:
                db.add(await _make_reapproval(
                    "application_owner", owner["email"], owner.get("name", owner["email"]),
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

    await aaudit(
        db, "order", new_order.id, "created",
        new=_order_snap(new_order),
        by=portal_actor_by(current_user, "portal_change_order"),
        ctx=f"modify_of:{order_id}",
        classification=await classify_for_asset_type_id(db, new_order.asset_type_id),
    )

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
                SET status = 'Free', current_order_id = NULL, expires_at = NULL
                WHERE id = :aid AND status = 'reserved'
            """), {"aid": original.assigned_asset_id})
            logger.info("Portal: Released reserved asset %s for cancelled order %s",
                        original.assigned_asset_id, order_id)
        original.status = OrderStatus.CANCELLED
        await aaudit(
            db, "order", original.id, "status_changed",
            old={"status": OrderStatus.SCHEDULED.value},
            new={"status": OrderStatus.CANCELLED.value},
            by=portal_actor_by(current_user, "portal_cancel_order"),
            classification=await classify_for_asset_type_id(db, original.asset_type_id),
        )
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
        # Inherit the requester AD snapshot for consistent chargeback
        requester_sam_account=original.requester_sam_account,
        requester_department=original.requester_department,
        requester_cost_center=original.requester_cost_center,
        requester_company=original.requester_company,
        requester_employee_id=original.requester_employee_id,
        requester_title=original.requester_title,
    )
    db.add(cancel_order)
    await db.flush()

    from app.routes.webhook import _dispatch_runbook
    cancel_order.celery_task_id = _dispatch_runbook(cancel_order)
    cancel_order.status = OrderStatus.PROCESSING

    # Mark original provision order as cancelled so it disappears from My IT
    original_old_status = original.status.value
    original.status = OrderStatus.CANCELLED

    # Two audit rows: the new DELETE order being created, and the
    # original provision order transitioning to CANCELLED. Same actor on
    # both, classification inherits from the asset type.
    cls = await classify_for_asset_type_id(db, original.asset_type_id)
    actor = portal_actor_by(current_user, "portal_cancel_order")
    await aaudit(
        db, "order", cancel_order.id, "created",
        new=_order_snap(cancel_order),
        by=actor, ctx=f"cancel_of:{order_id}",
        classification=cls,
    )
    await aaudit(
        db, "order", original.id, "status_changed",
        old={"status": original_old_status},
        new={"status": OrderStatus.CANCELLED.value},
        by=actor,
        classification=cls,
    )

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

    from app.utils.approval_decision import SoDViolation, apply_approval_decision
    try:
        await apply_approval_decision(
            db, approval, decision, comment,
            actor=portal_actor_by(current_user, "decide_approval"),
        )
    except SoDViolation as exc:
        # SoD is per-asset-type; the approval row stays ``pending`` so
        # another approver can decide. We return 409 with a message
        # quoting the original config-time attribution back at the
        # operator so the path forward is clear.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Separation of duties: you configured asset type "
                f"{exc.asset_type_id} (audit: {exc.audit_excerpt!r}). "
                f"Ask a different approver to decide on this order."
            ),
        ) from exc
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
                WHERE asset_type_id = :at AND status = 'Free'
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
