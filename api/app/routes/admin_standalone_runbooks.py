"""Admin-API: Standalone Runbooks (independent of asset types).

All endpoints require X-Admin-Key (via require_admin_key).
"""

import json as _json
import logging
from typing import Any

from celery import Celery
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func as sa_func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.script_module import ScriptModule
from app.models.standalone_runbook import (
    StandaloneRunbook,
    StandaloneRunbookRun,
    StandaloneRunbookRunStep,
    StandaloneRunbookStep,
)
from app.utils.auth import require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/standalone-runbooks",
    tags=["admin-standalone-runbooks"],
    dependencies=[Depends(require_admin_key)],
)

# Celery app (lazy import for trigger)
_celery_app: Celery | None = None


def _get_celery():
    global _celery_app
    if _celery_app is None:
        from tasks import app as celery_app
        _celery_app = celery_app
    return _celery_app


# ── Schemas ───────────────────────────────────────────────────────────────────

class RunbookCreate(BaseModel):
    name: str
    description: str | None = None
    is_active: bool = True
    cron_expression: str | None = None
    cron_enabled: bool = False
    skip_if_running: bool = True


class RunbookUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    cron_expression: str | None = None
    cron_enabled: bool | None = None
    skip_if_running: bool | None = None


class StepCreate(BaseModel):
    position: int
    step_name: str
    script_module_id: int
    params_template: dict[str, Any] | None = None
    is_critical: bool = True
    retry_count: int = 3
    timeout_seconds: int = 120


class StepUpdate(BaseModel):
    step_name: str | None = None
    script_module_id: int | None = None
    params_template: dict[str, Any] | None = None
    is_critical: bool | None = None
    retry_count: int | None = None
    timeout_seconds: int | None = None


class ReorderRequest(BaseModel):
    step_ids: list[int]


# ── Runbook CRUD ──────────────────────────────────────────────────────────────

@router.get("")
async def list_runbooks(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(
        select(StandaloneRunbook)
        .options(selectinload(StandaloneRunbook.steps))
        .order_by(StandaloneRunbook.name)
    )
    runbooks = result.scalars().all()

    # Fetch last run info per runbook
    last_runs = {}
    if runbooks:
        rb_ids = [rb.id for rb in runbooks]
        # Subquery for max run id per runbook
        sub = (
            select(
                StandaloneRunbookRun.runbook_id,
                sa_func.max(StandaloneRunbookRun.id).label("max_id"),
            )
            .where(StandaloneRunbookRun.runbook_id.in_(rb_ids))
            .group_by(StandaloneRunbookRun.runbook_id)
            .subquery()
        )
        runs_result = await db.execute(
            select(StandaloneRunbookRun)
            .join(sub, StandaloneRunbookRun.id == sub.c.max_id)
        )
        for run in runs_result.scalars().all():
            last_runs[run.runbook_id] = {
                "id": run.id,
                "status": run.status,
                "trigger": run.trigger,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }

    return [
        {
            "id": rb.id,
            "name": rb.name,
            "description": rb.description,
            "is_active": rb.is_active,
            "cron_expression": rb.cron_expression,
            "cron_enabled": rb.cron_enabled,
            "skip_if_running": rb.skip_if_running,
            "step_count": len(rb.steps),
            "last_run": last_runs.get(rb.id),
        }
        for rb in runbooks
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_runbook(body: RunbookCreate, db: AsyncSession = Depends(get_db)) -> dict:
    rb = StandaloneRunbook(
        name=body.name,
        description=body.description,
        is_active=body.is_active,
        cron_expression=body.cron_expression,
        cron_enabled=body.cron_enabled,
        skip_if_running=body.skip_if_running,
    )
    db.add(rb)
    await db.commit()
    await db.refresh(rb)
    return {"id": rb.id, "name": rb.name}


@router.get("/{runbook_id}")
async def get_runbook(runbook_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(StandaloneRunbook)
        .options(selectinload(StandaloneRunbook.steps).selectinload(StandaloneRunbookStep.script_module))
        .where(StandaloneRunbook.id == runbook_id)
    )
    rb = result.scalar_one_or_none()
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Standalone runbook {runbook_id} not found")
    return {
        "id": rb.id,
        "name": rb.name,
        "description": rb.description,
        "is_active": rb.is_active,
        "cron_expression": rb.cron_expression,
        "cron_enabled": rb.cron_enabled,
        "skip_if_running": rb.skip_if_running,
        "steps": [
            {
                "id": s.id,
                "position": s.position,
                "step_name": s.step_name,
                "script_module_id": s.script_module_id,
                "script_module_name": s.script_module.name if s.script_module else None,
                "params_template": s.params_template,
                "is_critical": s.is_critical,
                "retry_count": s.retry_count,
                "timeout_seconds": s.timeout_seconds,
            }
            for s in rb.steps
        ],
    }


@router.put("/{runbook_id}")
async def update_runbook(
    runbook_id: int, body: RunbookUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    rb = await db.get(StandaloneRunbook, runbook_id)
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Standalone runbook {runbook_id} not found")
    if body.name is not None:
        rb.name = body.name
    if body.description is not None:
        rb.description = body.description
    if body.is_active is not None:
        rb.is_active = body.is_active
    if body.cron_expression is not None:
        rb.cron_expression = body.cron_expression or None
    if body.cron_enabled is not None:
        rb.cron_enabled = body.cron_enabled
    if body.skip_if_running is not None:
        rb.skip_if_running = body.skip_if_running
    await db.commit()
    return {"id": runbook_id, "updated": True}


@router.delete("/{runbook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runbook(runbook_id: int, db: AsyncSession = Depends(get_db)) -> None:
    rb = await db.get(StandaloneRunbook, runbook_id)
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Standalone runbook {runbook_id} not found")
    await db.delete(rb)
    await db.commit()


# ── Steps CRUD ────────────────────────────────────────────────────────────────

@router.post("/{runbook_id}/steps", status_code=status.HTTP_201_CREATED)
async def add_step(
    runbook_id: int, body: StepCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    rb = await db.get(StandaloneRunbook, runbook_id)
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Standalone runbook {runbook_id} not found")
    module = await db.get(ScriptModule, body.script_module_id)
    if not module:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown script_module_id: {body.script_module_id}")

    await db.execute(
        text("""
            INSERT INTO standalone_runbook_steps
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


@router.put("/{runbook_id}/steps/{step_id}")
async def update_step(
    runbook_id: int, step_id: int, body: StepUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    s = await db.get(StandaloneRunbookStep, step_id)
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
            text("UPDATE standalone_runbook_steps SET params_template = CAST(:ptpl AS jsonb) WHERE id = :id"),
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


@router.delete("/{runbook_id}/steps", status_code=status.HTTP_204_NO_CONTENT)
async def delete_all_steps(runbook_id: int, db: AsyncSession = Depends(get_db)) -> None:
    await db.execute(
        text("DELETE FROM standalone_runbook_steps WHERE runbook_id = :rid"),
        {"rid": runbook_id},
    )
    await db.commit()


@router.delete("/{runbook_id}/steps/{step_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_step(
    runbook_id: int, step_id: int, db: AsyncSession = Depends(get_db)
) -> None:
    s = await db.get(StandaloneRunbookStep, step_id)
    if not s or s.runbook_id != runbook_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Step {step_id} not found")
    await db.delete(s)
    await db.commit()


@router.post("/{runbook_id}/steps/reorder")
async def reorder_steps(
    runbook_id: int, body: ReorderRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    for new_pos, step_id in enumerate(body.step_ids, start=1):
        await db.execute(
            text("UPDATE standalone_runbook_steps SET position = :pos WHERE id = :id AND runbook_id = :rid"),
            {"pos": new_pos, "id": step_id, "rid": runbook_id},
        )
    await db.commit()
    return {"reordered": True, "count": len(body.step_ids)}


# ── Execution ─────────────────────────────────────────────────────────────────

@router.post("/{runbook_id}/trigger")
async def trigger_runbook(runbook_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    rb = await db.get(StandaloneRunbook, runbook_id)
    if not rb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Standalone runbook {runbook_id} not found")
    if not rb.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Runbook is not active")

    run = StandaloneRunbookRun(
        runbook_id=runbook_id,
        trigger="manual",
        triggered_by="admin",
        status="pending",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    try:
        celery = _get_celery()
        task = celery.send_task(
            "tasks.workflows.standalone_runner.run",
            args=[run.id],
            queue="provision",
        )
        run.celery_task_id = task.id
        await db.commit()
    except Exception as exc:
        logger.error("Failed to dispatch standalone runbook run %s: %s", run.id, exc)
        run.status = "failed"
        run.error_message = str(exc)
        await db.commit()

    return {"run_id": run.id, "status": run.status}


@router.get("/{runbook_id}/runs")
async def list_runs(runbook_id: int, db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(
        select(StandaloneRunbookRun)
        .where(StandaloneRunbookRun.runbook_id == runbook_id)
        .order_by(StandaloneRunbookRun.id.desc())
        .limit(50)
    )
    return [
        {
            "id": r.id,
            "trigger": r.trigger,
            "triggered_by": r.triggered_by,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "error_message": r.error_message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in result.scalars().all()
    ]


@router.get("/{runbook_id}/runs/{run_id}")
async def get_run(runbook_id: int, run_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(StandaloneRunbookRun)
        .options(selectinload(StandaloneRunbookRun.run_steps))
        .where(StandaloneRunbookRun.id == run_id, StandaloneRunbookRun.runbook_id == runbook_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found")
    return {
        "id": r.id,
        "trigger": r.trigger,
        "triggered_by": r.triggered_by,
        "status": r.status,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "error_message": r.error_message,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "steps": [
            {
                "id": s.id,
                "step_name": s.step_name,
                "position": s.position,
                "status": s.status,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                "log_output": s.log_output,
                "error": s.error,
            }
            for s in r.run_steps
        ],
    }
