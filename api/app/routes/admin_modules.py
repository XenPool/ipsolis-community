"""Admin API: Script Modules and Global Variables management."""

import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.global_var import GlobalVar
from app.models.ps_module import PsModule
from app.models.runbook import RunbookStep
from app.models.script_module import ScriptModule
from app.utils.auth import require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin-modules"],
    dependencies=[Depends(require_admin_key)],
)

_SECRET_MASK = "***"


# ── Schemas ────────────────────────────────────────────────────────────────────

class ScriptModuleCreate(BaseModel):
    name: str
    description: str | None = None
    script_content: str = ""
    script_type: str = "powershell"
    param_schema: list[dict[str, Any]] | None = None
    is_active: bool = True


class ScriptModuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    script_content: str | None = None
    script_type: str | None = None
    param_schema: list[dict[str, Any]] | None = None
    is_active: bool | None = None


class GlobalVarCreate(BaseModel):
    key: str
    value: str | None = None
    description: str | None = None
    is_secret: bool = False


class GlobalVarUpdate(BaseModel):
    value: str | None = None
    description: str | None = None
    is_secret: bool | None = None


# ── Script Modules ─────────────────────────────────────────────────────────────

@router.get("/script-modules")
async def list_script_modules(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(select(ScriptModule).order_by(ScriptModule.name))
    modules = result.scalars().all()
    return [
        {
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "script_type": m.script_type,
            "param_count": len(m.param_schema) if m.param_schema else 0,
            "is_active": m.is_active,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        }
        for m in modules
    ]


@router.post("/script-modules", status_code=status.HTTP_201_CREATED)
async def create_script_module(
    payload: ScriptModuleCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    existing = await db.execute(select(ScriptModule).where(ScriptModule.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Module {payload.name!r} already exists",
        )
    module = ScriptModule(
        name=payload.name,
        description=payload.description,
        script_content=payload.script_content,
        script_type=payload.script_type,
        param_schema=payload.param_schema,
        is_active=payload.is_active,
    )
    db.add(module)
    await db.commit()
    await db.refresh(module)
    logger.info("admin: created script_module id=%s name=%s", module.id, module.name)
    return {"id": module.id, "name": module.name}


@router.get("/script-modules/{module_id}")
async def get_script_module(module_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    module = await db.get(ScriptModule, module_id)
    if not module:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Module {module_id} not found")
    return {
        "id": module.id,
        "name": module.name,
        "description": module.description,
        "script_content": module.script_content,
        "script_type": module.script_type,
        "param_schema": module.param_schema,
        "is_active": module.is_active,
        "created_at": module.created_at.isoformat() if module.created_at else None,
        "updated_at": module.updated_at.isoformat() if module.updated_at else None,
    }


@router.put("/script-modules/{module_id}")
async def update_script_module(
    module_id: int, payload: ScriptModuleUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    module = await db.get(ScriptModule, module_id)
    if not module:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Module {module_id} not found")
    if payload.name is not None:
        module.name = payload.name
    if payload.description is not None:
        module.description = payload.description
    if payload.script_content is not None:
        module.script_content = payload.script_content
    if payload.script_type is not None:
        module.script_type = payload.script_type
    if payload.param_schema is not None:
        await db.execute(
            text("UPDATE script_modules SET param_schema = CAST(:ps AS jsonb), updated_at = NOW() WHERE id = :id"),
            {"ps": __import__("json").dumps(payload.param_schema), "id": module_id},
        )
    if payload.is_active is not None:
        module.is_active = payload.is_active
    await db.execute(text("UPDATE script_modules SET updated_at = NOW() WHERE id = :id"), {"id": module_id})
    await db.commit()
    logger.info("admin: updated script_module id=%s", module_id)
    return {"id": module_id, "updated": True}


class ScriptModuleTestPayload(BaseModel):
    params: dict[str, Any] = {}


@router.post("/script-modules/{module_id}/test")
async def test_script_module(
    module_id: int,
    payload: ScriptModuleTestPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Enqueues a test run of the script module via Celery."""
    module = await db.get(ScriptModule, module_id)
    if not module:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Module {module_id} not found")
    # Import Celery app inline to avoid circular imports
    from celery import Celery as _Celery
    celery_app = _Celery()
    celery_app.config_from_object({"broker_url": os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")})
    task = celery_app.send_task(
        "tasks.workflows.dynamic_runner.test_script_module",
        kwargs={"script_module_id": module_id, "params": payload.params},
        queue="provision",
    )
    return {"task_id": task.id}


@router.get("/script-module-test/{task_id}")
async def get_test_result(task_id: str) -> dict:
    """Polls the result of a test_script_module Celery task."""
    import os
    from celery import Celery as _Celery
    celery_app = _Celery()
    celery_app.config_from_object({
        "broker_url": os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
        "result_backend": os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/0"),
    })
    res = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": res.status,
        "result": res.result if res.ready() else None,
    }


@router.delete("/script-modules/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script_module(module_id: int, db: AsyncSession = Depends(get_db)) -> None:
    module = await db.get(ScriptModule, module_id)
    if not module:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Module {module_id} not found")
    # Check if any runbook steps reference this module
    used = await db.execute(
        select(RunbookStep).where(RunbookStep.script_module_id == module_id).limit(1)
    )
    if used.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Module {module_id} is still referenced by runbook steps",
        )
    await db.delete(module)
    await db.commit()
    logger.info("admin: deleted script_module id=%s", module_id)


# ── Global Variables ───────────────────────────────────────────────────────────

def _mask_var(v: GlobalVar) -> dict:
    return {
        "id": v.id,
        "key": v.key,
        "value": _SECRET_MASK if v.is_secret else v.value,
        "description": v.description,
        "is_secret": v.is_secret,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


@router.get("/global-vars")
async def list_global_vars(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(select(GlobalVar).order_by(GlobalVar.key))
    return [_mask_var(v) for v in result.scalars().all()]


@router.post("/global-vars", status_code=status.HTTP_201_CREATED)
async def create_global_var(
    payload: GlobalVarCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    existing = await db.execute(select(GlobalVar).where(GlobalVar.key == payload.key))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Variable {payload.key!r} already exists",
        )
    var = GlobalVar(
        key=payload.key,
        value=payload.value,
        description=payload.description,
        is_secret=payload.is_secret,
    )
    db.add(var)
    await db.commit()
    await db.refresh(var)
    logger.info("admin: created global_var key=%s", payload.key)
    return _mask_var(var)


@router.put("/global-vars/{var_id}")
async def update_global_var(
    var_id: int, payload: GlobalVarUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    var = await db.get(GlobalVar, var_id)
    if not var:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Variable {var_id} not found")
    if payload.value is not None:
        var.value = payload.value
    if payload.description is not None:
        var.description = payload.description
    if payload.is_secret is not None:
        var.is_secret = payload.is_secret
    await db.execute(text("UPDATE global_vars SET updated_at = NOW() WHERE id = :id"), {"id": var_id})
    await db.commit()
    await db.refresh(var)
    return _mask_var(var)


@router.delete("/global-vars/{var_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_global_var(var_id: int, db: AsyncSession = Depends(get_db)) -> None:
    var = await db.get(GlobalVar, var_id)
    if not var:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Variable {var_id} not found")
    await db.delete(var)
    await db.commit()
    logger.info("admin: deleted global_var id=%s", var_id)


# ── PS Modules ─────────────────────────────────────────────────────────────────

class PsModuleCreate(BaseModel):
    name: str
    required_version: str | None = None


class PsModuleUpdate(BaseModel):
    required_version: str | None = None


def _ps_module_dict(m: PsModule) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "required_version": m.required_version,
        "status": m.status,
        "installed_version": m.installed_version,
        "error_log": m.error_log,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


def _enqueue_install(ps_module_id: int) -> str:
    from celery import Celery as _Celery
    celery_app = _Celery()
    celery_app.config_from_object({
        "broker_url": os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
        "result_backend": os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1"),
    })
    task = celery_app.send_task(
        "tasks.workflows.ps_module_installer.install_ps_module",
        kwargs={"ps_module_id": ps_module_id},
        queue="provision",
    )
    return task.id


@router.get("/ps-modules/search")
async def search_psgallery(q: str = "") -> list[dict]:
    """Search PSGallery for modules matching q (min 2 chars)."""
    if len(q) < 2:
        return []
    url = (
        "https://www.powershellgallery.com/api/v2/Search()"
        f"?$filter=IsLatestVersion&searchTerm='{q}'"
        "&$orderby=DownloadCount+desc&$top=15&includePrerelease=false"
    )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, headers={"Accept": "application/atom+xml"})
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("psgallery search failed for q=%r: %s", q, exc)
        return []

    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
        "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    }
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return []

    results = []
    for entry in root.findall("a:entry", ns):
        props = entry.find("m:properties", ns)
        if props is None:
            continue
        name = (props.findtext("d:Id", "", ns) or "").strip()
        version = (props.findtext("d:Version", "", ns) or "").strip()
        summary = (
            props.findtext("d:Summary", "", ns)
            or props.findtext("d:Description", "", ns)
            or ""
        ).strip()[:120]
        if name:
            results.append({"name": name, "version": version, "description": summary})
    return results


@router.get("/ps-modules")
async def list_ps_modules(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(select(PsModule).order_by(PsModule.name))
    return [_ps_module_dict(m) for m in result.scalars().all()]


@router.post("/ps-modules", status_code=status.HTTP_201_CREATED)
async def create_ps_module(
    payload: PsModuleCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    existing = await db.execute(select(PsModule).where(PsModule.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"PS module {payload.name!r} already exists",
        )
    m = PsModule(name=payload.name, required_version=payload.required_version, status="pending")
    db.add(m)
    await db.commit()
    await db.refresh(m)
    task_id = _enqueue_install(m.id)
    logger.info("admin: created ps_module id=%s name=%s task=%s", m.id, m.name, task_id)
    return {**_ps_module_dict(m), "task_id": task_id}


@router.put("/ps-modules/{ps_module_id}")
async def update_ps_module(
    ps_module_id: int, payload: PsModuleUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    m = await db.get(PsModule, ps_module_id)
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"PS module {ps_module_id} not found")
    m.required_version = payload.required_version
    await db.execute(text("UPDATE ps_modules SET status = 'pending', updated_at = NOW() WHERE id = :id"), {"id": ps_module_id})
    await db.commit()
    task_id = _enqueue_install(ps_module_id)
    logger.info("admin: updated ps_module id=%s task=%s", ps_module_id, task_id)
    return {**_ps_module_dict(m), "task_id": task_id}


@router.post("/ps-modules/{ps_module_id}/install")
async def reinstall_ps_module(ps_module_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    m = await db.get(PsModule, ps_module_id)
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"PS module {ps_module_id} not found")
    await db.execute(text("UPDATE ps_modules SET status = 'pending', updated_at = NOW() WHERE id = :id"), {"id": ps_module_id})
    await db.commit()
    task_id = _enqueue_install(ps_module_id)
    logger.info("admin: reinstall ps_module id=%s task=%s", ps_module_id, task_id)
    return {"task_id": task_id}


@router.get("/ps-module-install/{task_id}")
async def get_ps_module_install_result(task_id: str) -> dict:
    from celery import Celery as _Celery
    celery_app = _Celery()
    celery_app.config_from_object({
        "broker_url": os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
        "result_backend": os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1"),
    })
    res = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": res.status,
        "result": res.result if res.ready() else None,
    }


@router.delete("/ps-modules/{ps_module_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ps_module(ps_module_id: int, db: AsyncSession = Depends(get_db)) -> None:
    m = await db.get(PsModule, ps_module_id)
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"PS module {ps_module_id} not found")
    await db.delete(m)
    await db.commit()
    logger.info("admin: deleted ps_module id=%s name=%s", ps_module_id, m.name)
