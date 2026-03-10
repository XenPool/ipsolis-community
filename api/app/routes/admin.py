import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.asset import AssetPool, AssetStatus, AssetType
from app.models.audit import AuditLog
from app.models.config import AppConfig
from app.models.order import Order
from app.schemas.admin import (
    AppConfigCreate,
    AppConfigRead,
    AppConfigUpdate,
    AssetPoolCreate,
    AssetPoolUpdate,
    AssetTypeCreate,
    AssetTypeUpdate,
    AuditLogRead,
)
from app.schemas.asset import AssetPoolRead, AssetTypeRead
from app.utils.asset_type_constraints import validate_asset_type
from app.utils.audit import _asset_snap, _config_snap, _type_snap, aaudit
from app.utils.auth import require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
)

_SECRET_MASK = "***"


def _mask(cfg: AppConfig) -> AppConfigRead:
    """Gibt AppConfigRead zurück; maskiert den Wert wenn is_secret=True."""
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
    return _mask(cfg)


@router.delete("/config/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_config(key: str, db: AsyncSession = Depends(get_db)) -> None:
    result = await db.execute(select(AppConfig).where(AppConfig.key == key))
    cfg = result.scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Key {key!r} not found")
    await aaudit(db, "app_config", cfg.id, "deleted", old=_config_snap(cfg), by="api:delete_config")
    await db.delete(cfg)
    await db.commit()
    logger.info("admin: deleted config key=%s", key)


# ── Asset-Typen ────────────────────────────────────────────────────────────────

@router.post("/asset-types", response_model=AssetTypeRead, status_code=status.HTTP_201_CREATED)
async def create_asset_type(
    payload: AssetTypeCreate, db: AsyncSession = Depends(get_db)
) -> AssetType:
    existing = await db.execute(select(AssetType).where(AssetType.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Asset type {payload.name!r} already exists",
        )
    violations = validate_asset_type(
        assignment_model=payload.assignment_model,
        automation_strategy=payload.automation_strategy,
        deprovision_policy=payload.deprovision_policy,
        personal_provisioning_strategy=payload.personal_provisioning_strategy,
        runbook_provision_id=payload.runbook_provision_id,
        runbook_revoke_id=payload.runbook_revoke_id,
        skip_runbook_rules=True,  # runbooks can't exist before the asset type has an ID
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
        allow_user_lists=payload.allow_user_lists,
        deprovision_policy=payload.deprovision_policy,
        personal_provisioning_strategy=payload.personal_provisioning_strategy,
        naming_pattern=payload.naming_pattern,
        max_per_user=payload.max_per_user,
        automation_strategy=payload.automation_strategy,
        composite_steps=payload.composite_steps,
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
    result = await db.execute(select(AssetType).where(AssetType.id == type_id))
    asset_type = result.scalar_one_or_none()
    if not asset_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset type {type_id} not found")

    # Merge payload with current DB values to get effective configuration for validation.
    eff_assignment_model       = payload.assignment_model or asset_type.assignment_model
    eff_automation_strategy    = payload.automation_strategy or asset_type.automation_strategy
    eff_deprovision_policy     = payload.deprovision_policy or asset_type.deprovision_policy
    eff_pps                    = payload.personal_provisioning_strategy or asset_type.personal_provisioning_strategy

    # Runbook IDs: use payload value if supplied, otherwise look up existing runbooks in DB.
    eff_provision_id = payload.runbook_provision_id
    if eff_provision_id is None:
        rb = (await db.execute(
            text("SELECT id FROM runbook_definitions WHERE asset_type_id = :at AND action = 'provision' AND is_active = true LIMIT 1"),
            {"at": type_id},
        )).fetchone()
        eff_provision_id = rb[0] if rb else None

    eff_revoke_id = payload.runbook_revoke_id
    if eff_revoke_id is None:
        rb = (await db.execute(
            text("SELECT id FROM runbook_definitions WHERE asset_type_id = :at AND action = 'delete' AND is_active = true LIMIT 1"),
            {"at": type_id},
        )).fetchone()
        eff_revoke_id = rb[0] if rb else None

    violations = validate_asset_type(
        assignment_model=eff_assignment_model,
        automation_strategy=eff_automation_strategy,
        deprovision_policy=eff_deprovision_policy,
        personal_provisioning_strategy=eff_pps,
        runbook_provision_id=eff_provision_id,
        runbook_revoke_id=eff_revoke_id,
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
    if payload.allow_user_lists is not None:
        asset_type.allow_user_lists = payload.allow_user_lists
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
    await aaudit(db, "asset_type", asset_type.id, "updated", old=old_snap, new=_type_snap(asset_type), by="api:update_asset_type")
    await db.commit()
    await db.refresh(asset_type)
    logger.info("admin: updated asset_type id=%s", type_id)
    return asset_type


@router.delete("/asset-types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset_type(type_id: int, db: AsyncSession = Depends(get_db)) -> None:
    result = await db.execute(select(AssetType).where(AssetType.id == type_id))
    asset_type = result.scalar_one_or_none()
    if not asset_type:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset type {type_id} not found")

    # Prüfen ob noch Assets oder Orders verknüpft sind
    linked_assets = await db.execute(
        select(AssetPool).where(AssetPool.asset_type_id == type_id)
    )
    if linked_assets.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Asset type {type_id} still has assets in the pool",
        )
    linked_orders = await db.execute(
        select(Order).where(Order.asset_type_id == type_id)
    )
    if linked_orders.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Asset type {type_id} still has orders referencing it",
        )

    await aaudit(db, "asset_type", asset_type.id, "deleted", old=_type_snap(asset_type), by="api:delete_asset_type")
    await db.delete(asset_type)
    await db.commit()
    logger.info("admin: deleted asset_type id=%s", type_id)


# ── Asset-Pool ─────────────────────────────────────────────────────────────────

@router.post("/assets", response_model=AssetPoolRead, status_code=status.HTTP_201_CREATED)
async def create_asset(
    payload: AssetPoolCreate, db: AsyncSession = Depends(get_db)
) -> AssetPool:
    # Asset-Typ prüfen
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


@router.put("/assets/{asset_id}", response_model=AssetPoolRead)
async def update_asset(
    asset_id: int, payload: AssetPoolUpdate, db: AsyncSession = Depends(get_db)
) -> AssetPool:
    result = await db.execute(select(AssetPool).where(AssetPool.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset {asset_id} not found")
    old_snap = _asset_snap(asset)
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
    await aaudit(db, "asset", asset.id, "deleted", old=_asset_snap(asset), by="api:delete_asset")
    await db.delete(asset)
    await db.commit()
    logger.info("admin: deleted asset id=%s", asset_id)


@router.get("/assets")
async def list_assets(
    asset_type_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    q = (
        select(AssetPool, AssetType.name.label("type_name"))
        .join(AssetType, AssetPool.asset_type_id == AssetType.id)
        .order_by(AssetPool.asset_type_id, AssetPool.name)
    )
    if asset_type_id:
        q = q.where(AssetPool.asset_type_id == asset_type_id)
    rows = (await db.execute(q)).all()
    result = []
    for asset, type_name in rows:
        d = _asset_snap(asset)
        d["type_name"] = type_name
        d["last_reclaim_at"] = asset.last_reclaim_at.isoformat() if asset.last_reclaim_at else None
        d["asset_metadata"] = asset.asset_metadata or {}
        result.append(d)
    return result


# ── Audit-Log ──────────────────────────────────────────────────────────────────

@router.get("/audit-log", response_model=list[AuditLogRead])
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
