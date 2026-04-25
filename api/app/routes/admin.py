import asyncio
import functools
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.asset import AssetPool, AssetStatus, AssetType
from app.models.audit import AuditLog
from app.models.config import AppConfig
from app.models.order import Order, OrderStep
from app.schemas.admin import (
    AppConfigCreate,
    AppConfigRead,
    AppConfigUpdate,
    AssetBulkCreate,
    AssetPoolCreate,
    AssetPoolUpdate,
    AssetTypeCreate,
    AssetTypeUpdate,
    AuditLogRead,
    ForceDeleteAsset,
)
from app.schemas.asset import AssetPoolRead, AssetTypeRead
from app.utils.asset_type_constraints import validate_asset_type
from app.utils.audit import _asset_snap, _config_snap, _type_snap, aaudit
from app.utils.auth import require_admin_key
from app.utils.features import require_enterprise
from app.utils.license import is_feature_enabled
from app.templates_instance import set_app_title, set_app_logo_config

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
)

_SECRET_MASK = "***"


def _enterprise_gate_for_config_key(key: str) -> str | None:
    """Return the feature name to check for a given app_config key, or None if Community.

    Keys in `app_config` reused for Enterprise-only customisations are gated here so
    community users cannot bypass the dedicated endpoints by writing to /config.
    """
    if key == "app.title" or key.startswith("app.logo"):
        return "app_branding"
    if key.startswith("email.tpl."):
        return "email_template_editor"
    if key.startswith("global."):
        return "global_variables"
    return None


def _require_config_key_licensed(key: str) -> None:
    feature = _enterprise_gate_for_config_key(key)
    if feature and not is_feature_enabled(feature):
        label = feature.replace("_", " ").title()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{label} requires an Ipsolis Enterprise license. "
                f"Contact info@xenpool.com for licensing options."
            ),
        )


def _require_asset_type_fields_licensed(payload) -> None:
    """Reject payloads setting Enterprise-only asset-type fields under Community license.

    Accepts either AssetTypeCreate or AssetTypeUpdate (both Pydantic). Fields that
    are None / False / empty are treated as unset and pass.
    """
    violations: list[str] = []
    if getattr(payload, "eligible_requestors_dn", None):
        violations.append("eligible_requestors")
    if getattr(payload, "allow_rdp_users", False):
        violations.append("eligible_requestors")
    if getattr(payload, "allow_admin_users", False):
        violations.append("eligible_requestors")
    if getattr(payload, "requires_approval_on_modify", False):
        violations.append("reapproval_on_modify")
    if getattr(payload, "requires_owner_approval", False):
        violations.append("app_owner_approval")
    if getattr(payload, "deprovision_policy", "") == "custom_runbook":
        violations.append("custom_deprovision")

    blocked = [f for f in violations if not is_feature_enabled(f)]
    if blocked:
        # Dedup while preserving order
        seen: set[str] = set()
        unique = [f for f in blocked if not (f in seen or seen.add(f))]
        labels = ", ".join(f.replace("_", " ").title() for f in unique)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{labels} require an Ipsolis Enterprise license. "
                f"Contact info@xenpool.com for licensing options."
            ),
        )


def _mask(cfg: AppConfig) -> AppConfigRead:
    """Returns AppConfigRead; masks the value if is_secret=True."""
    return AppConfigRead(
        id=cfg.id,
        key=cfg.key,
        value=_SECRET_MASK if cfg.is_secret else cfg.value,
        description=cfg.description,
        is_secret=cfg.is_secret,
        updated_at=cfg.updated_at,
    )


# ── app_config ─────────────────────────────────────────────────────────────────

@router.get("/config", response_model=list[AppConfigRead])
async def list_config(db: AsyncSession = Depends(get_db)) -> list[AppConfigRead]:
    result = await db.execute(select(AppConfig).order_by(AppConfig.key))
    rows = result.scalars().all()
    return [_mask(r) for r in rows]


@router.get("/config/{key}", response_model=AppConfigRead)
async def get_config(key: str, db: AsyncSession = Depends(get_db)) -> AppConfigRead:
    result = await db.execute(select(AppConfig).where(AppConfig.key == key))
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Key {key!r} not found")
    return _mask(cfg)


@router.post("/config", response_model=AppConfigRead, status_code=status.HTTP_201_CREATED)
async def create_config(
    payload: AppConfigCreate, db: AsyncSession = Depends(get_db)
) -> AppConfigRead:
    _require_config_key_licensed(payload.key)
    existing = await db.execute(select(AppConfig).where(AppConfig.key == payload.key))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Key {payload.key!r} already exists",
        )
    cfg = AppConfig(
        key=payload.key,
        value=payload.value,
        description=payload.description,
        is_secret=payload.is_secret,
    )
    db.add(cfg)
    await db.flush()
    await aaudit(db, "app_config", cfg.id, "created", new=_config_snap(cfg), by="api:create_config")
    await db.commit()
    await db.refresh(cfg)
    logger.info("admin: created config key=%s", payload.key)
    return _mask(cfg)


@router.put("/config/{key}", response_model=AppConfigRead)
async def update_config(
    key: str, payload: AppConfigUpdate, db: AsyncSession = Depends(get_db)
) -> AppConfigRead:
    _require_config_key_licensed(key)
    result = await db.execute(select(AppConfig).where(AppConfig.key == key))
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Key {key!r} not found")
    old_snap = _config_snap(cfg)
    cfg.value = payload.value
    if payload.description is not None:
        cfg.description = payload.description
    await aaudit(db, "app_config", cfg.id, "updated", old=old_snap, new=_config_snap(cfg), by="api:update_config")
    await db.commit()
    await db.refresh(cfg)
    logger.info("admin: updated config key=%s", key)
    if key == "app.title":
        set_app_title(cfg.value)
    elif key in ("app.logo", "app.logo_position", "app.logo_size", "app.logo_show_title", "app.logo_title_size"):
        set_app_logo_config(key, cfg.value)
    return _mask(cfg)


@router.delete("/config/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_config(key: str, db: AsyncSession = Depends(get_db)) -> None:
    _require_config_key_licensed(key)
    result = await db.execute(select(AppConfig).where(AppConfig.key == key))
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Key {key!r} not found")
    await aaudit(db, "app_config", cfg.id, "deleted", old=_config_snap(cfg), by="api:delete_config")
    await db.delete(cfg)
    await db.commit()
    logger.info("admin: deleted config key=%s", key)


@router.post("/config/ad/test")
async def test_ad_connection(db: AsyncSession = Depends(get_db)) -> dict:
    """Tries to bind to AD with current ad.* config and returns a status dict."""
    from sqlalchemy import text as sa_text
    from urllib.parse import quote

    async def _get(key: str, default: str = "") -> str:
        r = await db.execute(sa_text("SELECT value FROM app_config WHERE key = :k"), {"k": key})
        row = r.fetchone()
        return row[0] if row and row[0] else default

    server_host = await _get("ad.server", "dc.example.com")
    server_port = int(await _get("ad.port", "389"))
    bind_user = await _get("ad.username", "")
    bind_password = await _get("ad.password", "")
    domain = await _get("ad.domain", "")
    base_dn = await _get("ad.base_dn", "DC=example,DC=com")

    raw_user = f"{domain}\\{bind_user}" if domain else bind_user
    url = (f"ldap+ntlm-password://{quote(raw_user, safe='')}:"
           f"{quote(bind_password, safe='')}@{server_host}:{server_port}")

    try:
        from msldap.commons.factory import LDAPConnectionFactory
        factory = LDAPConnectionFactory.from_url(url)
        client = factory.get_client()
        await client.connect()
        count = 0
        async for _, err in client.pagedsearch("(objectClass=user)", ["sAMAccountName"],
                                               tree=base_dn):
            if err:
                break
            count += 1
            if count >= 1:
                break
        await client.disconnect()
        return {"ok": True, "message": f"Bind successful. Found {count} user(s) in search."}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@router.post("/config/entra/test")
async def test_entra_connection(db: AsyncSession = Depends(get_db)) -> dict:
    """Verifies Entra ID credentials by acquiring an app-only token (client credentials flow)."""
    from app.utils.entra import _get_entra_config, get_msal_app

    cfg = await _get_entra_config(db)
    mode = cfg.get("entra.mode", "disabled")

    if mode == "disabled":
        return {"ok": None, "message": "Entra ID mode is set to 'disabled' – no test performed."}

    msal_app = get_msal_app(cfg)
    if msal_app is None:
        return {"ok": False, "message": "Missing tenant_id, client_id, or client_secret."}

    try:
        import msal
        # Client credentials flow: acquires an app-only token to verify credentials
        result = msal_app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" in result:
            return {"ok": True, "message": "Entra ID credentials valid – app token acquired successfully."}
        error = result.get("error_description") or result.get("error") or "Unknown error"
        return {"ok": False, "message": f"Token acquisition failed: {error}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@router.post("/config/sccm/test", dependencies=[require_enterprise("sccm_integration")])
async def test_sccm_connection() -> dict:
    """Enqueues a Celery task that runs a pwsh+Kerberos probe inside the worker
    container (where krb5 libs and pwsh are installed) and waits for the result."""
    import asyncio

    from celery import Celery

    broker = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    backend = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1")
    client = Celery("api-client", broker=broker, backend=backend)

    def _enqueue_and_wait() -> dict:
        try:
            async_result = client.send_task("tasks.workflows.sccm_probe.probe", queue="provision")
            return async_result.get(timeout=45)
        except Exception as exc:
            return {"ok": False, "message": f"Probe task did not complete: {exc}"}

    return await asyncio.get_running_loop().run_in_executor(None, _enqueue_and_wait)


# ── Asset-Typen ────────────────────────────────────────────────────────────────

@router.get("/asset-types/{type_id}/logo", include_in_schema=False)
async def asset_type_logo(type_id: int, db: AsyncSession = Depends(get_db)):
    """Serves an asset type logo image from the DB (for admin preview)."""
    import base64
    from fastapi.responses import Response
    result = await db.execute(select(AssetType).where(AssetType.id == type_id))
    at = result.scalar_one_or_none()
    if not at or not at.logo:
        raise HTTPException(status_code=404, detail="No logo")
    try:
        header, b64_data = at.logo.split(",", 1)
        mime = header.split(":")[1].split(";")[0]
        raw = base64.b64decode(b64_data)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid logo data")
    return Response(content=raw, media_type=mime, headers={"Cache-Control": "no-cache"})


@router.post("/asset-types", response_model=AssetTypeRead, status_code=status.HTTP_201_CREATED)
async def create_asset_type(
    payload: AssetTypeCreate, db: AsyncSession = Depends(get_db)
) -> AssetType:
    _require_asset_type_fields_licensed(payload)
    existing = await db.execute(select(AssetType).where(AssetType.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Asset type {payload.name!r} already exists",
        )
    violations = validate_asset_type(
        category=payload.category.value,
        assignment_model=payload.assignment_model,
        automation_strategy=payload.automation_strategy,
        deprovision_policy=payload.deprovision_policy,
        personal_provisioning_strategy=payload.personal_provisioning_strategy,
    )
    if violations:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"code": v.code, "message": v.message} for v in violations],
        )
    asset_type = AssetType(
        name=payload.name,
        description=payload.description,
        category=payload.category,
        config=payload.config,
        assignment_model=payload.assignment_model,
        pool_capacity=payload.pool_capacity,
        automation_mode=payload.automation_mode,
        targets=payload.targets,
        lifecycle_ttl_days=payload.lifecycle_ttl_days,
        lifecycle_renewable=payload.lifecycle_renewable,
        lifecycle_reminder_days=payload.lifecycle_reminder_days,
        allow_rdp_users=payload.allow_rdp_users,
        allow_admin_users=payload.allow_admin_users,
        rds_gateway_url=payload.rds_gateway_url,
        deprovision_policy=payload.deprovision_policy,
        personal_provisioning_strategy=payload.personal_provisioning_strategy,
        naming_pattern=payload.naming_pattern,
        max_per_user=payload.max_per_user,
        automation_strategy=payload.automation_strategy,
        composite_steps=payload.composite_steps,
        requires_manager_approval=payload.requires_manager_approval,
        requires_owner_approval=payload.requires_owner_approval,
        approval_owners=payload.approval_owners,
        requires_approval_on_modify=payload.requires_approval_on_modify,
        eligible_requestors_dn=payload.eligible_requestors_dn or None,
        logo=payload.logo or None,
    )
    db.add(asset_type)
    await db.flush()
    await aaudit(db, "asset_type", asset_type.id, "created", new=_type_snap(asset_type), by="api:create_asset_type")
    await db.commit()
    await db.refresh(asset_type)
    logger.info("admin: created asset_type id=%s name=%s", asset_type.id, asset_type.name)
    return asset_type


@router.put("/asset-types/{type_id}", response_model=AssetTypeRead)
async def update_asset_type(
    type_id: int, payload: AssetTypeUpdate, db: AsyncSession = Depends(get_db)
) -> AssetType:
    _require_asset_type_fields_licensed(payload)
    result = await db.execute(select(AssetType).where(AssetType.id == type_id))
    asset_type = result.scalar_one_or_none()
    if not asset_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset type {type_id} not found")

    # Merge payload with current DB values to get effective configuration for validation.
    eff_category               = (payload.category.value if payload.category else None) or asset_type.category
    eff_assignment_model       = payload.assignment_model or asset_type.assignment_model
    eff_automation_strategy    = payload.automation_strategy or asset_type.automation_strategy
    eff_deprovision_policy     = payload.deprovision_policy or asset_type.deprovision_policy
    eff_pps                    = payload.personal_provisioning_strategy or asset_type.personal_provisioning_strategy

    violations = validate_asset_type(
        category=eff_category,
        assignment_model=eff_assignment_model,
        automation_strategy=eff_automation_strategy,
        deprovision_policy=eff_deprovision_policy,
        personal_provisioning_strategy=eff_pps,
    )
    if violations:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"code": v.code, "message": v.message} for v in violations],
        )

    old_snap = _type_snap(asset_type)
    if payload.name is not None:
        asset_type.name = payload.name
    if payload.description is not None:
        asset_type.description = payload.description
    if payload.category is not None:
        asset_type.category = payload.category
    if payload.config is not None:
        asset_type.config = payload.config
    if payload.assignment_model is not None:
        asset_type.assignment_model = payload.assignment_model
    if payload.pool_capacity is not None:
        asset_type.pool_capacity = payload.pool_capacity
    if payload.automation_mode is not None:
        asset_type.automation_mode = payload.automation_mode
    if payload.targets is not None:
        asset_type.targets = payload.targets
    if payload.lifecycle_ttl_days is not None:
        asset_type.lifecycle_ttl_days = payload.lifecycle_ttl_days
    if payload.lifecycle_renewable is not None:
        asset_type.lifecycle_renewable = payload.lifecycle_renewable
    if payload.lifecycle_reminder_days is not None:
        asset_type.lifecycle_reminder_days = payload.lifecycle_reminder_days
    if payload.allow_rdp_users is not None:
        asset_type.allow_rdp_users = payload.allow_rdp_users
    if payload.allow_admin_users is not None:
        asset_type.allow_admin_users = payload.allow_admin_users
    if payload.rds_gateway_url is not None:
        asset_type.rds_gateway_url = payload.rds_gateway_url or None
    if payload.deprovision_policy is not None:
        asset_type.deprovision_policy = payload.deprovision_policy
    if payload.personal_provisioning_strategy is not None:
        asset_type.personal_provisioning_strategy = payload.personal_provisioning_strategy
    if payload.naming_pattern is not None:
        asset_type.naming_pattern = payload.naming_pattern
    if payload.max_per_user is not None:
        asset_type.max_per_user = payload.max_per_user
    if payload.automation_strategy is not None:
        asset_type.automation_strategy = payload.automation_strategy
    if payload.composite_steps is not None:
        asset_type.composite_steps = payload.composite_steps
    if payload.requires_manager_approval is not None:
        asset_type.requires_manager_approval = payload.requires_manager_approval
    if payload.requires_owner_approval is not None:
        asset_type.requires_owner_approval = payload.requires_owner_approval
    if payload.approval_owners is not None:
        asset_type.approval_owners = payload.approval_owners or None
    if payload.requires_approval_on_modify is not None:
        asset_type.requires_approval_on_modify = payload.requires_approval_on_modify
    if payload.eligible_requestors_dn is not None:
        asset_type.eligible_requestors_dn = payload.eligible_requestors_dn or None
    if payload.logo is not None:
        asset_type.logo = payload.logo or None
    await aaudit(db, "asset_type", asset_type.id, "updated", old=old_snap, new=_type_snap(asset_type), by="api:update_asset_type")
    await db.commit()
    await db.refresh(asset_type)
    logger.info("admin: updated asset_type id=%s", type_id)
    return asset_type


@router.post("/asset-types/{type_id}/clone", response_model=AssetTypeRead, status_code=status.HTTP_201_CREATED)
async def clone_asset_type(type_id: int, db: AsyncSession = Depends(get_db)) -> AssetType:
    """Shallow-clone an asset type: same configuration, fresh row, name suffixed.

    Runbooks (provision / modify / deprovision) and pool assets are NOT
    copied — those reference the source type and would need a deeper clone.
    The cloned row starts with empty runbook slots, which the admin then
    configures or copies in separately.

    Name collision is resolved by appending " (copy)", " (copy 2)", etc.
    """
    result = await db.execute(select(AssetType).where(AssetType.id == type_id))
    src = result.scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset type {type_id} not found")

    # Find a unique name. "Foo" -> "Foo (copy)" -> "Foo (copy 2)" -> ...
    base = f"{src.name} (copy)"
    candidate = base
    suffix = 2
    while True:
        clash = await db.execute(select(AssetType.id).where(AssetType.name == candidate))
        if clash.scalar_one_or_none() is None:
            break
        candidate = f"{base} {suffix}"
        suffix += 1

    new_type = AssetType(
        name=candidate,
        description=src.description,
        category=src.category,
        config=src.config,
        assignment_model=src.assignment_model,
        pool_capacity=src.pool_capacity,
        automation_mode=src.automation_mode,
        targets=src.targets,
        lifecycle_ttl_days=src.lifecycle_ttl_days,
        lifecycle_renewable=src.lifecycle_renewable,
        lifecycle_reminder_days=src.lifecycle_reminder_days,
        allow_rdp_users=src.allow_rdp_users,
        allow_admin_users=src.allow_admin_users,
        rds_gateway_url=src.rds_gateway_url,
        deprovision_policy=src.deprovision_policy,
        personal_provisioning_strategy=src.personal_provisioning_strategy,
        naming_pattern=src.naming_pattern,
        max_per_user=src.max_per_user,
        automation_strategy=src.automation_strategy,
        composite_steps=src.composite_steps,
        requires_manager_approval=src.requires_manager_approval,
        requires_owner_approval=src.requires_owner_approval,
        approval_owners=src.approval_owners,
        requires_approval_on_modify=src.requires_approval_on_modify,
        eligible_requestors_dn=src.eligible_requestors_dn,
        logo=src.logo,
    )
    db.add(new_type)
    await db.flush()
    await aaudit(db, "asset_type", new_type.id, "cloned", new=_type_snap(new_type),
                 by=f"api:clone_asset_type from id={src.id}")
    await db.commit()
    await db.refresh(new_type)
    logger.info("admin: cloned asset_type id=%s -> id=%s name=%r", src.id, new_type.id, new_type.name)
    return new_type


@router.delete("/asset-types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset_type(type_id: int, db: AsyncSession = Depends(get_db)) -> None:
    result = await db.execute(select(AssetType).where(AssetType.id == type_id))
    asset_type = result.scalar_one_or_none()
    if not asset_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset type {type_id} not found")

    # Cascade-delete all related data so deletion always succeeds.
    # 1. Collect order IDs for this asset type
    order_ids = list(
        (await db.execute(select(Order.id).where(Order.asset_type_id == type_id))).scalars().all()
    )
    if order_ids:
        # 2. Nullify current_order_id on assets that point to these orders
        await db.execute(
            AssetPool.__table__.update()
            .where(AssetPool.__table__.c.current_order_id.in_(order_ids))
            .values(current_order_id=None)
        )
        # 3. Delete order steps (no DB cascade defined)
        await db.execute(
            OrderStep.__table__.delete().where(OrderStep.__table__.c.order_id.in_(order_ids))
        )
        # 4. Delete orders (order_change_log cascades via FK)
        await db.execute(
            Order.__table__.delete().where(Order.__table__.c.id.in_(order_ids))
        )
    # 5. Delete assets in the pool for this type (asset_type_id is NOT NULL)
    await db.execute(
        AssetPool.__table__.delete().where(AssetPool.__table__.c.asset_type_id == type_id)
    )
    # 6. runbook_definitions/steps cascade via FK ondelete=CASCADE
    await aaudit(db, "asset_type", asset_type.id, "deleted", old=_type_snap(asset_type), by="api:delete_asset_type")
    await db.delete(asset_type)
    await db.commit()
    logger.info("admin: deleted asset_type id=%s", type_id)


# ── Asset-Pool ─────────────────────────────────────────────────────────────────

@router.post("/assets", response_model=AssetPoolRead, status_code=status.HTTP_201_CREATED)
async def create_asset(
    payload: AssetPoolCreate, db: AsyncSession = Depends(get_db)
) -> AssetPool:
    # Validate asset type
    type_result = await db.execute(select(AssetType).where(AssetType.id == payload.asset_type_id))
    if not type_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Asset type {payload.asset_type_id} not found",
        )
    existing = await db.execute(select(AssetPool).where(AssetPool.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Asset {payload.name!r} already exists",
        )
    asset = AssetPool(
        name=payload.name,
        asset_type_id=payload.asset_type_id,
        status=payload.status,
        asset_metadata=payload.asset_metadata,
    )
    db.add(asset)
    await db.flush()
    await aaudit(db, "asset", asset.id, "created", new=_asset_snap(asset), by="api:create_asset")
    await db.commit()
    await db.refresh(asset)
    logger.info("admin: created asset id=%s name=%s", asset.id, asset.name)
    return asset


@router.post("/assets/bulk")
async def bulk_create_assets(payload: AssetBulkCreate, db: AsyncSession = Depends(get_db)) -> dict:
    """Create multiple assets at once. Skips duplicates, collects errors per item."""
    # Validate all referenced type IDs exist (batch lookup)
    type_ids = {item.asset_type_id for item in payload.items}
    rows = (await db.execute(select(AssetType.id).where(AssetType.id.in_(type_ids)))).scalars().all()
    valid_type_ids = set(rows)

    # Fetch existing names to detect duplicates efficiently
    names = [item.name for item in payload.items]
    existing_names = set(
        (await db.execute(select(AssetPool.name).where(AssetPool.name.in_(names)))).scalars().all()
    )

    created: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []

    for item in payload.items:
        if item.asset_type_id not in valid_type_ids:
            errors.append({"name": item.name, "error": f"Asset type {item.asset_type_id} not found"})
            continue
        if item.name in existing_names:
            skipped.append(item.name)
            continue
        meta: dict = {}
        if item.notes:
            meta["notes"] = item.notes
        asset = AssetPool(
            name=item.name,
            asset_type_id=item.asset_type_id,
            status=AssetStatus.FREE,
            asset_metadata=meta if meta else None,
        )
        db.add(asset)
        created.append(item.name)
        existing_names.add(item.name)  # prevent intra-batch duplicates

    if created:
        await db.flush()
        await db.commit()
    logger.info("admin: bulk created %d assets, skipped %d", len(created), len(skipped))
    return {"created": created, "skipped": skipped, "errors": errors}


@router.put("/assets/{asset_id}", response_model=AssetPoolRead)
async def update_asset(
    asset_id: int, payload: AssetPoolUpdate, db: AsyncSession = Depends(get_db)
) -> AssetPool:
    result = await db.execute(select(AssetPool).where(AssetPool.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset {asset_id} not found")
    old_snap = _asset_snap(asset)
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name must not be empty")
        if new_name != asset.name:
            clash = await db.execute(
                select(AssetPool.id).where(AssetPool.name == new_name, AssetPool.id != asset_id)
            )
            if clash.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Asset with name {new_name!r} already exists",
                )
            asset.name = new_name
    if payload.status is not None:
        asset.status = payload.status
    if payload.asset_metadata is not None:
        asset.asset_metadata = payload.asset_metadata
    if payload.expires_at is not None:
        asset.expires_at = payload.expires_at
    action = "status_changed" if payload.status is not None else "updated"
    await aaudit(db, "asset", asset.id, action, old=old_snap, new=_asset_snap(asset), by="api:update_asset")
    await db.commit()
    await db.refresh(asset)
    logger.info("admin: updated asset id=%s", asset_id)
    return asset


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(asset_id: int, db: AsyncSession = Depends(get_db)) -> None:
    result = await db.execute(select(AssetPool).where(AssetPool.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset {asset_id} not found")
    if asset.status not in (AssetStatus.FREE, AssetStatus.MAINTENANCE):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Asset {asset_id} has status {asset.status.value!r} – only FREE or MAINTENANCE assets can be deleted",
        )
    await db.execute(
        Order.__table__.update()
        .where(Order.__table__.c.assigned_asset_id == asset_id)
        .values(assigned_asset_id=None)
    )
    await aaudit(db, "asset", asset.id, "deleted", old=_asset_snap(asset), by="api:delete_asset")
    await db.delete(asset)
    await db.commit()
    logger.info("admin: deleted asset id=%s", asset_id)


# ── Force-delete (works for any status, optional permission revoke) ────────────

_SYNC_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://xpuser:changeme@db:5432/ipsolis",
).replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _sync_revoke(user_email: str, asset_type_id: int) -> dict:
    """Run target_executor.revoke() synchronously in its own DB session.
    Called via run_in_executor so it doesn't block the event loop.
    """
    import sys, pathlib
    # Ensure worker package is importable from the API container
    worker_path = str(pathlib.Path("/app/worker"))
    if worker_path not in sys.path:
        sys.path.insert(0, worker_path)
    from tasks.modules import target_executor  # noqa: PLC0415

    engine = create_engine(_SYNC_DB_URL, pool_pre_ping=True)
    with Session(engine) as db_sync:
        return target_executor.revoke(db_sync, user_email, asset_type_id)


@router.post("/assets/{asset_id}/force-delete", status_code=status.HTTP_204_NO_CONTENT)
async def force_delete_asset(
    asset_id: int,
    payload: ForceDeleteAsset,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Force-delete an asset regardless of its current status.

    If revoke_permissions=True and the asset has an active order, the
    user's group memberships are revoked via target_executor before deletion.
    All orders referencing this asset are cancelled and unlinked.
    """
    result = await db.execute(
        select(AssetPool).where(AssetPool.id == asset_id)
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset {asset_id} not found")

    snap = _asset_snap(asset)

    # Resolve the active order (if any) so we can revoke and cancel it
    active_order: Order | None = None
    if asset.current_order_id:
        ord_result = await db.execute(
            select(Order).where(Order.id == asset.current_order_id)
        )
        active_order = ord_result.scalar_one_or_none()

    # Optionally revoke group permissions
    revoke_result: dict | None = None
    if payload.revoke_permissions and active_order:
        try:
            revoke_result = await asyncio.get_event_loop().run_in_executor(
                None,
                functools.partial(
                    _sync_revoke,
                    active_order.user_email,
                    asset.asset_type_id,
                ),
            )
            logger.info(
                "admin force-delete: revoke result for asset %s user %s: %s",
                asset_id, active_order.user_email, revoke_result,
            )
        except Exception as exc:
            # Log but don't abort — admin explicitly wants the asset gone
            logger.error("admin force-delete: revoke failed for asset %s: %s", asset_id, exc)
            revoke_result = {"success": False, "error": str(exc)}

    # Cancel all non-final orders for this asset, including active delivered/provisioned ones
    # so the user no longer sees the asset under My IT.
    keep = {"revoked", "failed", "expired", "cancelled"}
    all_orders_result = await db.execute(
        select(Order).where(Order.assigned_asset_id == asset_id)
    )
    for order in all_orders_result.scalars().all():
        if order.status.value not in keep:
            order.status = "cancelled"  # type: ignore[assignment]
            order.error_message = "Cancelled by admin via force-delete"

    # Unlink asset from all orders
    await db.execute(
        Order.__table__.update()
        .where(Order.__table__.c.assigned_asset_id == asset_id)
        .values(assigned_asset_id=None)
    )

    audit_extra = {"force": True, "revoke_permissions": payload.revoke_permissions}
    if revoke_result is not None:
        audit_extra["revoke_result"] = revoke_result
    await aaudit(db, "asset", asset.id, "force_deleted", old=snap, new=audit_extra, by="api:force_delete_asset")
    await db.delete(asset)
    await db.commit()
    logger.info("admin: force-deleted asset id=%s (revoke=%s)", asset_id, payload.revoke_permissions)


@router.post("/assets/{asset_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_asset(
    asset_id: int,
    payload: ForceDeleteAsset,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke permissions for an active asset and return it to FREE status.

    The asset is NOT deleted. The active order is cancelled and unlinked so the
    user no longer sees it under My IT. If revoke_permissions=True, AD group
    memberships are removed via target_executor.
    """
    result = await db.execute(select(AssetPool).where(AssetPool.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset {asset_id} not found")

    snap = _asset_snap(asset)

    # Resolve the active order so we can revoke and cancel it
    active_order: Order | None = None
    if asset.current_order_id:
        ord_result = await db.execute(select(Order).where(Order.id == asset.current_order_id))
        active_order = ord_result.scalar_one_or_none()

    # Optionally revoke group permissions
    revoke_result: dict | None = None
    if payload.revoke_permissions and active_order:
        try:
            revoke_result = await asyncio.get_event_loop().run_in_executor(
                None,
                functools.partial(_sync_revoke, active_order.user_email, asset.asset_type_id),
            )
            logger.info("admin revoke-asset: revoke result for asset %s user %s: %s",
                        asset_id, active_order.user_email, revoke_result)
        except Exception as exc:
            logger.error("admin revoke-asset: revoke failed for asset %s: %s", asset_id, exc)
            revoke_result = {"success": False, "error": str(exc)}

    # Cancel all non-final orders and unlink them from the asset
    keep = {"revoked", "failed", "expired", "cancelled"}
    all_orders_result = await db.execute(select(Order).where(Order.assigned_asset_id == asset_id))
    for order in all_orders_result.scalars().all():
        if order.status.value not in keep:
            order.status = "cancelled"  # type: ignore[assignment]
            order.error_message = "Released by admin"

    await db.execute(
        Order.__table__.update()
        .where(Order.__table__.c.assigned_asset_id == asset_id)
        .values(assigned_asset_id=None)
    )

    # Return asset to free pool
    asset.status = AssetStatus.FREE  # type: ignore[assignment]
    asset.current_order_id = None
    asset.expires_at = None

    audit_extra = {"revoke_permissions": payload.revoke_permissions}
    if revoke_result is not None:
        audit_extra["revoke_result"] = revoke_result
    await aaudit(db, "asset", asset.id, "revoked", old=snap, new=audit_extra, by="api:revoke_asset")
    await db.commit()
    logger.info("admin: revoked asset id=%s back to free (revoke_permissions=%s)", asset_id, payload.revoke_permissions)


@router.get("/assets")
async def list_assets(
    asset_type_id: int | None = None,
    include_capacity_pooled: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    # By default, hide capacity_pooled types: their "slots" are tracked via
    # orders + pool_capacity on the asset type, not per-row. Pool rows for
    # these types (if any exist) are dead weight in the Asset Pool view.
    q = (
        select(AssetPool, AssetType.name.label("type_name"))
        .join(AssetType, AssetPool.asset_type_id == AssetType.id)
        .order_by(AssetPool.asset_type_id, AssetPool.name)
    )
    if asset_type_id:
        q = q.where(AssetPool.asset_type_id == asset_type_id)
    elif not include_capacity_pooled:
        q = q.where(AssetType.assignment_model != "capacity_pooled")
    rows = (await db.execute(q)).all()

    # Fetch user info for assets that have an active order
    order_ids = [a.current_order_id for a, _ in rows if a.current_order_id]
    user_by_order: dict[int, dict] = {}
    if order_ids:
        ord_rows = (await db.execute(
            select(Order.id, Order.user_email, Order.user_name).where(Order.id.in_(order_ids))
        )).all()
        user_by_order = {r[0]: {"email": r[1], "name": r[2]} for r in ord_rows}

    result = []
    for asset, type_name in rows:
        d = _asset_snap(asset)
        d["type_name"] = type_name
        d["last_reclaim_at"] = asset.last_reclaim_at.isoformat() if asset.last_reclaim_at else None
        d["asset_metadata"] = asset.asset_metadata or {}
        order_user = user_by_order.get(asset.current_order_id, {}) if asset.current_order_id else {}
        d["user_email"] = order_user.get("email")
        d["user_name"] = order_user.get("name")
        result.append(d)
    return result


# ── Audit-Log ──────────────────────────────────────────────────────────────────

@router.get("/audit-log", response_model=list[AuditLogRead], dependencies=[require_enterprise("audit_log_viewer")])
async def list_audit_log(
    entity_type: str | None = None,
    entity_id: int | None = None,
    triggered_by: str | None = None,
    from_ts: datetime | None = None,
    until_ts: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[AuditLog]:
    if limit > 500:
        limit = 500

    query = select(AuditLog)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(AuditLog.entity_id == entity_id)
    if triggered_by:
        query = query.where(AuditLog.triggered_by.contains(triggered_by))
    if from_ts:
        query = query.where(AuditLog.timestamp >= from_ts)
    if until_ts:
        query = query.where(AuditLog.timestamp <= until_ts)

    query = query.order_by(AuditLog.timestamp.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


# ── Email Templates ─────────────────────────────────────────────────────────────

@router.get("/email-templates", dependencies=[require_enterprise("email_template_editor")])
async def list_email_templates(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Lists all email templates (without body, for table display)."""
    from sqlalchemy import text as sa_text
    rows = (await db.execute(sa_text(
        "SELECT id, event_key, description, subject, is_active, updated_at "
        "FROM email_templates ORDER BY event_key"
    ))).fetchall()
    return [
        {
            "id": r[0], "event_key": r[1], "description": r[2],
            "subject": r[3], "is_active": r[4],
            "updated_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


@router.get("/email-templates/{event_key}", dependencies=[require_enterprise("email_template_editor")])
async def get_email_template(event_key: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Returns a single email template including body and available_variables."""
    from sqlalchemy import text as sa_text
    row = (await db.execute(
        sa_text(
            "SELECT id, event_key, description, subject, body, available_variables, is_active, updated_at "
            "FROM email_templates WHERE event_key = :k"
        ),
        {"k": event_key},
    )).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Template {event_key!r} not found")
    return {
        "id": row[0], "event_key": row[1], "description": row[2],
        "subject": row[3], "body": row[4],
        "available_variables": row[5] or [],
        "is_active": row[6],
        "updated_at": row[7].isoformat() if row[7] else None,
    }


@router.put("/email-templates/{event_key}", dependencies=[require_enterprise("email_template_editor")])
async def update_email_template(
    event_key: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Updates subject, body, and/or is_active for an email template."""
    from sqlalchemy import text as sa_text
    row = (await db.execute(
        sa_text("SELECT id FROM email_templates WHERE event_key = :k"),
        {"k": event_key},
    )).fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Template {event_key!r} not found")

    fields: list[str] = []
    params: dict = {"k": event_key}
    if "subject" in payload:
        fields.append("subject = :subject")
        params["subject"] = payload["subject"]
    if "body" in payload:
        fields.append("body = :body")
        params["body"] = payload["body"]
    if "is_active" in payload:
        fields.append("is_active = :is_active")
        params["is_active"] = bool(payload["is_active"])
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No fields to update")

    fields.append("updated_at = NOW()")
    await db.execute(
        sa_text(f"UPDATE email_templates SET {', '.join(fields)} WHERE event_key = :k"),
        params,
    )
    await db.commit()
    logger.info("admin: updated email_template event_key=%s fields=%s", event_key, list(payload.keys()))
    return {"ok": True, "event_key": event_key}


# ── Email Test ──────────────────────────────────────────────────────────────────

@router.post("/config/email/test", dependencies=[require_enterprise("email_template_editor")])
async def test_email(payload: dict = None, db: AsyncSession = Depends(get_db)) -> dict:
    """Sends a test email using the current email.* config settings."""
    import asyncio
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from sqlalchemy import text as sa_text

    async def _get(key: str, default: str = "") -> str:
        r = await db.execute(sa_text("SELECT value FROM app_config WHERE key = :k"), {"k": key})
        row = r.fetchone()
        return row[0] if row and row[0] else default

    smtp_host = await _get("email.smtp_server", "localhost")
    smtp_port = int(await _get("email.smtp_port", "25"))
    smtp_user = await _get("email.username", "")
    smtp_password = await _get("email.password", "")
    mail_from = await _get("email.from", "noreply@example.com")
    from_name = await _get("email.from_name", "Ipsolis")
    bcc = await _get("email.bcc", "")
    app_title = await _get("app.title", "Ipsolis")

    to_address = (payload or {}).get("to") or bcc or mail_from

    subject = f"[{app_title}] SMTP connectivity check"
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f4f4f4;padding:20px;">
<table width="620" style="background:#fff;border-radius:4px;margin:0 auto;">
  <tr><td style="background:#1e3a8a;padding:24px 32px;color:#fff;font-size:20px;font-weight:bold;">
    {app_title}
  </td></tr>
  <tr><td style="padding:28px 32px;font-size:14px;color:#333;">
    <p>This is a verification email from the {app_title} system.</p>
    <p>If you received this, your SMTP configuration is working correctly.</p>
  </td></tr>
  <tr><td style="background:#f8f8f8;padding:16px 32px;font-size:11px;color:#aaa;text-align:center;">
    {app_title} | This email was generated automatically.
  </td></tr>
</table>
</body></html>"""

    def _send():
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{mail_from}>"
        msg["To"] = to_address
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_port == 587:
                server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.sendmail(mail_from, [to_address], msg.as_string())

    try:
        await asyncio.get_event_loop().run_in_executor(None, _send)
        return {"ok": True, "message": f"Test email sent to {to_address}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@router.get("/_validate-user", response_class=HTMLResponse)
async def admin_validate_user(request: Request, q: str = ""):
    """AD user validation endpoint for admin forms (same as portal's _validate-user)."""
    from app.utils.ad_lookup import lookup_user

    if not q.strip():
        return HTMLResponse("")
    result = lookup_user(q.strip())
    if result.get("success"):
        name = result.get("display_name", q)
        email = result.get("email", q)
        return HTMLResponse(
            f'<span class="text-xs text-green-700" data-valid="true" data-email="{email}" data-name="{name}">'
            f'&#10003; {name} ({email})</span>'
        )
    error = result.get("error", "Not found")
    return HTMLResponse(
        f'<span class="text-xs text-red-600" data-valid="false">&#10007; {error}</span>'
    )


