import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.asset import AssetPool, AssetStatus, AssetType
from app.models.audit import AuditLog
from app.models.config import AppConfig
from app.models.order import Order, OrderStep
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
    await db.execute(
        Order.__table__.update()
        .where(Order.__table__.c.assigned_asset_id == asset_id)
        .values(assigned_asset_id=None)
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


# ── Email Templates ─────────────────────────────────────────────────────────────

@router.get("/email-templates")
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


@router.get("/email-templates/{event_key}")
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


@router.put("/email-templates/{event_key}")
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

@router.post("/config/email/test")
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
    from_name = await _get("email.from_name", "XenPool IT Selfservice")
    bcc = await _get("email.bcc", "")
    company_name = await _get("company.name", "XenPool")

    to_address = (payload or {}).get("to") or bcc or mail_from

    subject = f"[{company_name}] Test Email"
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f4f4f4;padding:20px;">
<table width="620" style="background:#fff;border-radius:4px;margin:0 auto;">
  <tr><td style="background:#BB0A30;padding:24px 32px;color:#fff;font-size:20px;font-weight:bold;">
    {company_name} IT Self-Service
  </td></tr>
  <tr><td style="padding:28px 32px;font-size:14px;color:#333;">
    <p>This is a test email from the {company_name} IT Self-Service system.</p>
    <p>If you received this, your SMTP configuration is working correctly.</p>
  </td></tr>
  <tr><td style="background:#f8f8f8;padding:16px 32px;font-size:11px;color:#aaa;text-align:center;">
    {company_name} IT Self-Service | This email was generated automatically.
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
