"""Admin-API: Asset Types und Runbook-Verwaltung.

Alle Endpunkte erfordern X-Admin-Key (via require_admin_key).
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.asset import AssetType
from app.models.runbook import RunbookDefinition, RunbookStep
from app.models.script_module import ScriptModule
from app.utils.auth import require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin-runbooks"],
    dependencies=[Depends(require_admin_key)],
)


# ── Schemas ────────────────────────────────────────────────────────────────────
# TODO: These local schemas bypass the ORM handler in admin.py and therefore do
# NOT run the 5-rule constraint validation from app.utils.asset_type_constraints.
# For constraint parity, wire validate_asset_type() into the create/update
# handlers below (around lines 121–229). See admin.py for the reference impl.

class AssetTypeCreate(BaseModel):
    name: str
    description: str | None = None
    category: str = "platform_access"
    assignment_model: str = "assigned_personal"
    pool_capacity: int | None = None
    config: list[dict[str, Any]] | None = None
    automation_mode: str = "runbook"
    targets: list[dict[str, Any]] | None = None
    lifecycle_ttl_days: int | None = None
    lifecycle_renewable: bool = True


class AssetTypeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    assignment_model: str | None = None
    pool_capacity: int | None = None
    config: list[dict[str, Any]] | None = None
    automation_mode: str | None = None
    targets: list[dict[str, Any]] | None = None
    lifecycle_ttl_days: int | None = None
    lifecycle_renewable: bool | None = None


class RunbookCreate(BaseModel):
    name: str
    description: str | None = None
    asset_type_id: int
    action: str
    is_active: bool = True


class RunbookUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


class RunbookStepCreate(BaseModel):
    position: int
    step_name: str
    script_module_id: int
    params_template: dict[str, Any] | None = None
    is_critical: bool = True
    retry_count: int = 3
    timeout_seconds: int = 120


class RunbookStepUpdate(BaseModel):
    step_name: str | None = None
    script_module_id: int | None = None
    params_template: dict[str, Any] | None = None
    is_critical: bool | None = None
    retry_count: int | None = None
    timeout_seconds: int | None = None


class ReorderRequest(BaseModel):
    step_ids: list[int]  # Neue Reihenfolge der Step-IDs (von oben nach unten)


# ── Asset Types ────────────────────────────────────────────────────────────────

@router.get("/asset-types")
async def list_asset_types(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(
        select(AssetType).order_by(AssetType.name)
    )
    types = result.scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "category": t.category.value if hasattr(t.category, "value") else t.category,
            "assignment_model": t.assignment_model,
            "pool_capacity": t.pool_capacity,
            "config": t.config,
            "automation_mode": t.automation_mode,
            "targets": t.targets,
            "lifecycle_ttl_days": t.lifecycle_ttl_days,
            "lifecycle_renewable": t.lifecycle_renewable,
        }
        for t in types
    ]


@router.post("/asset-types", status_code=status.HTTP_201_CREATED)
async def create_asset_type(
    body: AssetTypeCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    import json as _json
    await db.execute(
        text("""
            INSERT INTO asset_types
                (name, description, category, assignment_model, pool_capacity, config,
                 automation_mode, targets, lifecycle_ttl_days, lifecycle_renewable,
                 created_at, updated_at)
            VALUES (
                :name, :desc,
                CAST(:cat AS asset_category),
                :amodel, :cap,
                CAST(:cfg AS jsonb),
                :amode,
                CAST(:tgts AS jsonb),
                :ttl, :renewable,
                NOW(), NOW()
            )
        """),
        {
            "name": body.name,
            "desc": body.description,
            "cat": body.category,
            "amodel": body.assignment_model,
            "cap": body.pool_capacity,
            "cfg": _json.dumps(body.config) if body.config else "null",
            "amode": body.automation_mode,
            "tgts": _json.dumps(body.targets) if body.targets else "null",
            "ttl": body.lifecycle_ttl_days,
            "renewable": body.lifecycle_renewable,
        },
    )
    await db.commit()
    result = await db.execute(select(AssetType).where(AssetType.name == body.name))
    t = result.scalar_one()
    return {"id": t.id, "name": t.name}


@router.get("/asset-types/{type_id}")
async def get_asset_type(type_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    t = await db.get(AssetType, type_id)
    if not t:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"AssetType {type_id} not found")
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "category": t.category.value if hasattr(t.category, "value") else t.category,
        "assignment_model": t.assignment_model,
        "pool_capacity": t.pool_capacity,
        "config": t.config,
        "automation_mode": t.automation_mode,
        "targets": t.targets,
        "lifecycle_ttl_days": t.lifecycle_ttl_days,
        "lifecycle_renewable": t.lifecycle_renewable,
    }


@router.put("/asset-types/{type_id}")
async def update_asset_type(
    type_id: int,
    body: AssetTypeUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    import json as _json
    t = await db.get(AssetType, type_id)
    if not t:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"AssetType {type_id} not found")

    if body.name is not None:
        t.name = body.name
    if body.description is not None:
        t.description = body.description
    if body.category is not None:
        await db.execute(
            text("UPDATE asset_types SET category = CAST(:cat AS asset_category), updated_at = NOW() WHERE id = :id"),
            {"cat": body.category, "id": type_id},
        )
    if body.assignment_model is not None:
        t.assignment_model = body.assignment_model
    if body.pool_capacity is not None:
        t.pool_capacity = body.pool_capacity
    if body.config is not None:
        await db.execute(
            text("UPDATE asset_types SET config = CAST(:cfg AS jsonb), updated_at = NOW() WHERE id = :id"),
            {"cfg": _json.dumps(body.config), "id": type_id},
        )
    if body.automation_mode is not None:
        t.automation_mode = body.automation_mode
    if body.targets is not None:
        await db.execute(
            text("UPDATE asset_types SET targets = CAST(:tgts AS jsonb), updated_at = NOW() WHERE id = :id"),
            {"tgts": _json.dumps(body.targets), "id": type_id},
        )
    if body.lifecycle_ttl_days is not None:
        t.lifecycle_ttl_days = body.lifecycle_ttl_days
    if body.lifecycle_renewable is not None:
        t.lifecycle_renewable = body.lifecycle_renewable

    await db.execute(
        text("UPDATE asset_types SET updated_at = NOW() WHERE id = :id"),
        {"id": type_id},
    )
    await db.commit()
    return {"id": type_id, "updated": True}


@router.delete("/asset-types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset_type(type_id: int, db: AsyncSession = Depends(get_db)) -> None:
    t = await db.get(AssetType, type_id)
    if not t:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"AssetType {type_id} not found")
    await db.delete(t)
    await db.commit()


# ── Runbooks ───────────────────────────────────────────────────────────────────

@router.get("/runbooks")
async def list_runbooks(
    asset_type_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    query = select(RunbookDefinition).options(selectinload(RunbookDefinition.steps))
    if asset_type_id:
        query = query.where(RunbookDefinition.asset_type_id == asset_type_id)
    query = query.order_by(RunbookDefinition.asset_type_id, RunbookDefinition.action)
    result = await db.execute(query)
    runbooks = result.scalars().all()
    return [
        {
            "id": rb.id,
            "name": rb.name,
            "description": rb.description,
            "asset_type_id": rb.asset_type_id,
            "action": rb.action,
            "is_active": rb.is_active,
            "step_count": len(rb.steps),
        }
        for rb in runbooks
    ]


@router.post("/runbooks", status_code=status.HTTP_201_CREATED)
async def create_runbook(
    body: RunbookCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    rb = RunbookDefinition(
        name=body.name,
        description=body.description,
        asset_type_id=body.asset_type_id,
        action=body.action,
        is_active=body.is_active,
    )
    db.add(rb)
    await db.commit()
    await db.refresh(rb)
    return {"id": rb.id, "name": rb.name}


@router.get("/runbooks/{runbook_id}")
async def get_runbook(runbook_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(RunbookDefinition)
        .options(selectinload(RunbookDefinition.steps))
        .where(RunbookDefinition.id == runbook_id)
    )
    rb = result.scalar_one_or_none()
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Runbook {runbook_id} not found")
    return {
        "id": rb.id,
        "name": rb.name,
        "description": rb.description,
        "asset_type_id": rb.asset_type_id,
        "action": rb.action,
        "is_active": rb.is_active,
        "steps": [
            {
                "id": s.id,
                "position": s.position,
                "step_name": s.step_name,
                "script_module_id": s.script_module_id,
                "module_key": s.module_key,
                "params_template": s.params_template,
                "is_critical": s.is_critical,
                "retry_count": s.retry_count,
                "timeout_seconds": s.timeout_seconds,
            }
            for s in rb.steps
        ],
    }


@router.put("/runbooks/{runbook_id}")
async def update_runbook(
    runbook_id: int,
    body: RunbookUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    rb = await db.get(RunbookDefinition, runbook_id)
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Runbook {runbook_id} not found")
    if body.name is not None:
        rb.name = body.name
    if body.description is not None:
        rb.description = body.description
    if body.is_active is not None:
        rb.is_active = body.is_active
    await db.commit()
    return {"id": runbook_id, "updated": True}


@router.delete("/runbooks/{runbook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runbook(runbook_id: int, db: AsyncSession = Depends(get_db)) -> None:
    rb = await db.get(RunbookDefinition, runbook_id)
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Runbook {runbook_id} not found")
    await db.delete(rb)
    await db.commit()


# ── Runbook Steps ──────────────────────────────────────────────────────────────

@router.post("/runbooks/{runbook_id}/steps", status_code=status.HTTP_201_CREATED)
async def add_step(
    runbook_id: int,
    body: RunbookStepCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    rb = await db.get(RunbookDefinition, runbook_id)
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Runbook {runbook_id} not found")
    module = await db.get(ScriptModule, body.script_module_id)
    if not module:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown script_module_id: {body.script_module_id}")

    import json as _json
    await db.execute(
        text("""
            INSERT INTO runbook_steps
                (runbook_id, position, step_name, script_module_id, params_template,
                 is_critical, retry_count, timeout_seconds, created_at)
            VALUES (:rid, :pos, :sname, :smid, CAST(:ptpl AS jsonb),
                    :critical, :retry, :timeout, NOW())
        """),
        {
            "rid": runbook_id,
            "pos": body.position,
            "sname": body.step_name,
            "smid": body.script_module_id,
            "ptpl": _json.dumps(body.params_template) if body.params_template else "null",
            "critical": body.is_critical,
            "retry": body.retry_count,
            "timeout": body.timeout_seconds,
        },
    )
    await db.commit()
    return {"created": True}


@router.put("/runbooks/{runbook_id}/steps/{step_id}")
async def update_step(
    runbook_id: int,
    step_id: int,
    body: RunbookStepUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    import json as _json
    s = await db.get(RunbookStep, step_id)
    if not s or s.runbook_id != runbook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Step {step_id} not found")
    if body.script_module_id is not None:
        module = await db.get(ScriptModule, body.script_module_id)
        if not module:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown script_module_id: {body.script_module_id}")
        s.script_module_id = body.script_module_id
    if body.step_name is not None:
        s.step_name = body.step_name
    if body.params_template is not None:
        await db.execute(
            text("UPDATE runbook_steps SET params_template = CAST(:ptpl AS jsonb) WHERE id = :id"),
            {"ptpl": _json.dumps(body.params_template), "id": step_id},
        )
    if body.is_critical is not None:
        s.is_critical = body.is_critical
    if body.retry_count is not None:
        s.retry_count = body.retry_count
    if body.timeout_seconds is not None:
        s.timeout_seconds = body.timeout_seconds
    await db.commit()
    return {"id": step_id, "updated": True}


@router.delete("/runbooks/{runbook_id}/steps/{step_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_step(
    runbook_id: int,
    step_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    s = await db.get(RunbookStep, step_id)
    if not s or s.runbook_id != runbook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Step {step_id} not found")
    await db.delete(s)
    await db.commit()


@router.post("/runbooks/{runbook_id}/steps/reorder")
async def reorder_steps(
    runbook_id: int,
    body: ReorderRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Setzt die Positionen der Steps gemäß der übergebenen ID-Reihenfolge."""
    for new_pos, step_id in enumerate(body.step_ids, start=1):
        await db.execute(
            text("UPDATE runbook_steps SET position = :pos WHERE id = :id AND runbook_id = :rid"),
            {"pos": new_pos, "id": step_id, "rid": runbook_id},
        )
    await db.commit()
    return {"reordered": True, "count": len(body.step_ids)}


# ── Module Registry (DB-driven) ────────────────────────────────────────────────

@router.get("/modules")
async def list_modules(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Returns active script modules from the database."""
    result = await db.execute(
        select(ScriptModule).where(ScriptModule.is_active.is_(True)).order_by(ScriptModule.name)
    )
    return [
        {
            "id": m.id,
            "name": m.name,
            "description": m.description,
            "script_type": m.script_type,
            "param_schema": m.param_schema,
        }
        for m in result.scalars().all()
    ]
