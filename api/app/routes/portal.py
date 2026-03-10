"""User Self-Service Portal – HTML-Routen.

Kein Admin-Key erforderlich (internes Tool, kein Login für MVP).
Aktionen: Neue VDI bestellen, Verlängern, RDP-/Admin-User ändern.
"""
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.asset import AssetPool, AssetType
from app.models.order import Order, OrderAction, OrderStatus
from app.utils.ad_lookup import lookup_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="/app/app/templates")

_STATUS_COLORS = {
    "pending":      "bg-gray-100 text-gray-700",
    "processing":   "bg-blue-100 text-blue-700",
    "provisioning": "bg-blue-100 text-blue-700",
    "delivered":    "bg-green-100 text-green-700",
    "provisioned":  "bg-green-100 text-green-700",
    "revoking":     "bg-orange-100 text-orange-700",
    "revoked":      "bg-gray-100 text-gray-500",
    "failed":       "bg-red-100 text-red-700",
    "expired":      "bg-orange-100 text-orange-700",
    "cancelled":    "bg-gray-100 text-gray-500",
}

_STEP_COLORS = {
    "pending": "bg-gray-100 text-gray-600",
    "running": "bg-blue-100 text-blue-700",
    "success": "bg-green-100 text-green-700",
    "failed":  "bg-red-100 text-red-700",
    "skipped": "bg-gray-100 text-gray-400",
}


# ── Übersicht ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def portal_index(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.steps))
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
        "orders": orders,
        "asset_type_names": asset_type_names,
        "asset_type_categories": asset_type_categories,
        "asset_names": asset_names,
        "status_colors": _STATUS_COLORS,
    })


# ── Neue Bestellung ────────────────────────────────────────────────────────────

@router.get("/bestellung/neu", response_class=HTMLResponse)
async def portal_new_order_form(request: Request, db: AsyncSession = Depends(get_db)):
    types_result = await db.execute(select(AssetType).order_by(AssetType.name))
    asset_types = list(types_result.scalars().all())
    return templates.TemplateResponse("portal/bestellung_neu.html", {
        "request": request,
        "active_page": "new",
        "asset_types": asset_types,
        "today": date.today().isoformat(),
        "error": None,
    })


def _validate_order_attrs(
    form_data,
    attr_defs: list[dict],
) -> tuple[dict | None, str | None]:
    """Validiert attr_*-Felder gegen AttributeDefinitions aus AssetType.config.

    Gibt (collected_config, None) bei Erfolg oder (None, error_msg) bei Fehler zurück.
    Nicht-sichtbare Felder (visibleWhen nicht erfüllt) werden ignoriert.
    """
    from app.schemas.admin import AttributeDefinition, AttributeType

    if not attr_defs:
        return None, None

    # Konvertiere raw dicts → AttributeDefinition (ignoriert ungültige Einträge)
    defs: list[AttributeDefinition] = []
    for raw in attr_defs:
        try:
            # Backwards-compat: altes Format ohne `type` → ENUM wenn options vorhanden
            if "type" not in raw:
                raw = dict(raw)
                raw["type"] = "ENUM" if raw.get("options") else "STRING"
            defs.append(AttributeDefinition.model_validate(raw))
        except Exception:
            pass  # Ungültige Definitionen überspringen

    collected: dict[str, object] = {}
    for attr in defs:
        # visibleWhen prüfen: falls Bedingung nicht erfüllt → Feld überspringen
        if attr.visible_when:
            cond_field = attr.visible_when.get("field", "")
            cond_value = attr.visible_when.get("value", "")
            form_val = form_data.get(cond_field, "")
            if str(form_val) != str(cond_value):
                continue  # nicht sichtbar → nicht validieren

        raw_val = form_data.getlist("attr_" + attr.key) if attr.type == AttributeType.MULTI_ENUM else form_data.get("attr_" + attr.key, "")

        # Pflichtfeld prüfen
        empty = (raw_val == "" or raw_val == [] or raw_val is None)
        if attr.required and empty:
            return None, f"Pflichtfeld '{attr.label}' wurde nicht ausgefüllt."

        if empty:
            if attr.default_value is not None:
                collected[attr.key] = attr.default_value
            continue

        # Typ-Konvertierung und Werteprüfung
        if attr.type == AttributeType.INT:
            try:
                collected[attr.key] = int(raw_val)
            except (ValueError, TypeError):
                return None, f"Feld '{attr.label}' muss eine ganze Zahl sein."
        elif attr.type == AttributeType.BOOL:
            collected[attr.key] = raw_val in ("true", "True", "1", "on", True)
        elif attr.type == AttributeType.ENUM:
            if attr.options and str(raw_val) not in attr.options:
                return None, f"Ungültiger Wert für '{attr.label}'."
            collected[attr.key] = str(raw_val)
        elif attr.type == AttributeType.MULTI_ENUM:
            vals = raw_val if isinstance(raw_val, list) else [raw_val]
            if attr.options:
                invalid = [v for v in vals if v not in attr.options]
                if invalid:
                    return None, f"Ungültige Werte für '{attr.label}': {', '.join(invalid)}"
            collected[attr.key] = vals
        else:
            collected[attr.key] = str(raw_val)

    return (collected if collected else None), None


@router.post("/bestellung/neu")
async def portal_create_order(
    request: Request,
    db: AsyncSession = Depends(get_db),
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
    # Alle Formular-Felder lesen (für attr_*-Felder)
    form_data = await request.form()

    async def _render_error(msg: str):
        types_result = await db.execute(select(AssetType).order_by(AssetType.name))
        return templates.TemplateResponse("portal/bestellung_neu.html", {
            "request": request,
            "active_page": "new",
            "asset_types": list(types_result.scalars().all()),
            "today": date.today().isoformat(),
            "error": msg,
            # Formular-Werte zurückgeben
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
        return await _render_error("Ungültiges Datumsformat.")

    if until_dt <= from_dt:
        return await _render_error("Das Enddatum muss nach dem Startdatum liegen.")

    # Asset-Typ laden für Attribut-Validierung
    at_result = await db.execute(select(AssetType).where(AssetType.id == asset_type_id))
    asset_type = at_result.scalar_one_or_none()
    if not asset_type:
        return await _render_error("Unbekannter Asset-Typ.")

    order_config, attr_error = _validate_order_attrs(form_data, asset_type.config or [])
    if attr_error:
        return await _render_error(attr_error)

    rdp_clean = [u.strip() for u in rdp_users if u.strip()]
    admin_clean = [u.strip() for u in admin_users if u.strip()]

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
        status=OrderStatus.PENDING,
        config=order_config,
    )
    db.add(order)
    await db.flush()

    from app.routes.webhook import _dispatch_runbook
    task_id = _dispatch_runbook(order)
    order.celery_task_id = task_id
    order.status = OrderStatus.PROCESSING
    await db.commit()

    logger.info("Portal: Order created id=%s user=%s", order.id, order.user_email)
    return RedirectResponse(url=f"/portal/bestellungen/{order.id}", status_code=303)


# ── Bestelldetail ──────────────────────────────────────────────────────────────

@router.get("/bestellungen/{order_id}", response_class=HTMLResponse)
async def portal_order_detail(
    request: Request,
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Order).options(selectinload(Order.steps)).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")

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

    return templates.TemplateResponse("portal/bestellung_detail.html", {
        "request": request,
        "active_page": "overview",
        "order": order,
        "asset_type": asset_type,
        "asset_type_name": asset_type_name,
        "asset_name": asset_name,
        "steps_with_duration": steps_with_duration,
        "status_colors": _STATUS_COLORS,
        "step_colors": _STEP_COLORS,
        "today": date.today().isoformat(),
    })


# ── Verlängern ─────────────────────────────────────────────────────────────────

@router.post("/bestellungen/{order_id}/extend")
async def portal_extend_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    new_until: str = Form(...),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")

    try:
        until_dt = datetime.fromisoformat(new_until).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="Ungültiges Datumsformat")

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
    return RedirectResponse(url=f"/portal/bestellungen/{new_order.id}", status_code=303)


# ── Zugang ändern ──────────────────────────────────────────────────────────────

@router.post("/bestellungen/{order_id}/modify")
async def portal_modify_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    rdp_users: list[str] = Form(default=[]),
    admin_users: list[str] = Form(default=[]),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")

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
    return RedirectResponse(url=f"/portal/bestellungen/{new_order.id}", status_code=303)


# ── Asset ändern (kombiniert: Laufzeit + User-Listen) ──────────────────────────

@router.post("/bestellungen/{order_id}/change")
async def portal_change_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    new_until: str | None = Form(default=None),
    rdp_users: list[str] = Form(default=[]),
    admin_users: list[str] = Form(default=[]),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")

    is_active = original.status in (OrderStatus.DELIVERED, OrderStatus.PROVISIONED)
    is_failed_change = (
        original.status == OrderStatus.FAILED
        and original.action in (OrderAction.MODIFY, OrderAction.EXTEND)
    )
    if not (is_active or is_failed_change):
        raise HTTPException(
            status_code=422,
            detail="Nur aktive Bestellungen (DELIVERED/PROVISIONED) können geändert werden",
        )

    # Resolve requested_until
    if new_until:
        try:
            requested_until = datetime.fromisoformat(new_until).replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=422, detail="Ungültiges Datumsformat")
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

    logger.info("Portal: Change order id=%s from order=%s", new_order.id, order_id)
    return RedirectResponse(url=f"/portal/bestellungen/{new_order.id}", status_code=303)


# ── Abbestellen ────────────────────────────────────────────────────────────────

@router.post("/bestellungen/{order_id}/cancel")
async def portal_cancel_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")

    if original.status not in (OrderStatus.DELIVERED, OrderStatus.PROVISIONED):
        raise HTTPException(
            status_code=422,
            detail="Nur aktive Bestellungen (DELIVERED/PROVISIONED) können abbestellt werden",
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
        # Snapshot aus der Provision-Order übernehmen → deterministischer Revoke
        provisioned_state=original.provisioned_state,
    )
    db.add(cancel_order)
    await db.flush()

    from app.routes.webhook import _dispatch_runbook
    cancel_order.celery_task_id = _dispatch_runbook(cancel_order)
    cancel_order.status = OrderStatus.PROCESSING
    await db.commit()

    logger.info("Portal: Cancel order id=%s from order=%s", cancel_order.id, order_id)
    return RedirectResponse(url=f"/portal/bestellungen/{cancel_order.id}", status_code=303)


# ── Meine IT – Übersicht aktiver Assets ────────────────────────────────────────

@router.get("/meine-it", response_class=HTMLResponse)
async def portal_meine_it(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.steps))
        .where(Order.action == OrderAction.PROVISION)
        .where(Order.status.in_([OrderStatus.DELIVERED, OrderStatus.PROVISIONED]))
        .order_by(Order.created_at.desc())
    )
    orders = list(result.scalars().all())

    type_ids = {o.asset_type_id for o in orders}
    asset_type_names: dict[int, str] = {}
    asset_type_categories: dict[int, str] = {}
    asset_types_by_id: dict[int, AssetType] = {}
    if type_ids:
        types_result = await db.execute(select(AssetType).where(AssetType.id.in_(type_ids)))
        for t in types_result.scalars().all():
            asset_type_names[t.id] = t.name
            asset_type_categories[t.id] = t.category.value
            asset_types_by_id[t.id] = t

    asset_ids = [o.assigned_asset_id for o in orders if o.assigned_asset_id]
    asset_names: dict[int, str] = {}
    if asset_ids:
        asset_rows = await db.execute(
            select(AssetPool.id, AssetPool.name).where(AssetPool.id.in_(asset_ids))
        )
        asset_names = {row.id: row.name for row in asset_rows}

    return templates.TemplateResponse("portal/meine_it.html", {
        "request": request,
        "active_page": "meine-it",
        "orders": orders,
        "asset_type_names": asset_type_names,
        "asset_type_categories": asset_type_categories,
        "asset_types_by_id": asset_types_by_id,
        "asset_names": asset_names,
        "status_colors": _STATUS_COLORS,
    })


@router.get("/meine-it/{order_id}", response_class=HTMLResponse)
async def portal_meine_it_detail(
    request: Request,
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")

    if order.status not in (OrderStatus.DELIVERED, OrderStatus.PROVISIONED):
        raise HTTPException(status_code=422, detail="Nur aktive Assets können hier verwaltet werden")

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

    return templates.TemplateResponse("portal/meine_it_detail.html", {
        "request": request,
        "active_page": "meine-it",
        "order": order,
        "asset_type": asset_type,
        "asset_type_name": asset_type_name,
        "asset_name": asset_name,
        "today": date.today().isoformat(),
        "status_colors": _STATUS_COLORS,
    })


# ── HTMX: User-Validierung ─────────────────────────────────────────────────────

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
