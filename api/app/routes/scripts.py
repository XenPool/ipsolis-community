"""Script-Editor API – PowerShell-Dateien im Browser verwalten und testen.

Alle Endpunkte erfordern X-Admin-Key (via require_admin_key).
- scripts/ivanti/ is read-only (reference scripts, do not modify)
- scripts/vsphere/, scripts/active_roles/ sind editierbar
"""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.config import settings
from app.utils.auth import require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/scripts",
    tags=["scripts"],
    dependencies=[Depends(require_admin_key)],
)

SCRIPTS_BASE = Path("/app/scripts")
READONLY_DIRS = {"ivanti"}  # scripts/ivanti/ is read-only reference
ALLOWED_EXTENSIONS = {".ps1", ".py", ".sh", ".txt"}

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _validate_path(category: str, name: str) -> Path:
    """Returns the validated path or raises HTTPException."""
    if not _SAFE_NAME.match(category) or not _SAFE_NAME.match(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid path")
    path = SCRIPTS_BASE / category / name
    # Security check: path must not escape the scripts directory
    try:
        path.relative_to(SCRIPTS_BASE)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path not allowed")
    return path


def _check_writable(category: str) -> None:
    if category in READONLY_DIRS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"scripts/{category}/ ist read-only (Referenz-Scripts)",
        )


# ── Verzeichnisstruktur ────────────────────────────────────────────────────────

@router.get("")
async def list_scripts() -> dict:
    """Returns the directory structure as JSON."""
    structure: dict[str, list] = {}

    if not SCRIPTS_BASE.exists():
        return {"categories": {}}

    for category_dir in sorted(SCRIPTS_BASE.iterdir()):
        if not category_dir.is_dir():
            continue
        cat = category_dir.name
        files = []
        for f in sorted(category_dir.iterdir()):
            if f.is_file() and f.suffix in ALLOWED_EXTENSIONS:
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "readonly": cat in READONLY_DIRS,
                })
        structure[cat] = files

    return {"categories": structure}


# ── Datei lesen ────────────────────────────────────────────────────────────────

@router.get("/{category}/{name}", response_class=PlainTextResponse)
async def read_script(category: str, name: str) -> str:
    path = _validate_path(category, name)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Datei nicht gefunden: {path.name}")
    return path.read_text(encoding="utf-8")


# ── Datei speichern ────────────────────────────────────────────────────────────

class ScriptContent(BaseModel):
    content: str


@router.put("/{category}/{name}")
async def save_script(category: str, name: str, body: ScriptContent) -> dict:
    _check_writable(category)
    path = _validate_path(category, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    logger.info("Script saved: %s", path)
    return {"saved": True, "path": str(path.relative_to(SCRIPTS_BASE))}


# ── Neue Datei anlegen ─────────────────────────────────────────────────────────

class NewScriptRequest(BaseModel):
    name: str
    content: str = ""


@router.post("/{category}", status_code=status.HTTP_201_CREATED)
async def create_script(category: str, body: NewScriptRequest) -> dict:
    _check_writable(category)
    path = _validate_path(category, body.name)
    if path.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, f"Datei existiert bereits: {body.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    logger.info("Script created: %s", path)
    return {"created": True, "path": str(path.relative_to(SCRIPTS_BASE))}


# ── Test-Runner ────────────────────────────────────────────────────────────────

class TestRunRequest(BaseModel):
    module_key: str
    params: dict = {}


@router.post("/test")
async def test_module(body: TestRunRequest) -> dict:
    """Dispatcht einen einzelnen Modul-Testlauf via Celery."""
    from celery import Celery
    celery_app = Celery(broker=settings.CELERY_BROKER_URL)
    result = celery_app.send_task(
        "tasks.workflows.dynamic_runner.test_module_run",
        args=[body.module_key, body.params],
        queue="provision",
    )
    return {"task_id": result.id}


@router.get("/test/{task_id}")
async def get_test_result(task_id: str) -> dict:
    """Pollt das Ergebnis eines Modul-Testlaufs."""
    from celery import Celery
    celery_app = Celery(
        broker=settings.CELERY_BROKER_URL,
        backend=settings.CELERY_RESULT_BACKEND,
    )
    result = celery_app.AsyncResult(task_id)
    if result.ready():
        return {
            "ready": True,
            "status": result.status,
            "result": result.get(timeout=1) if result.successful() else None,
            "error": str(result.result) if result.failed() else None,
        }
    return {"ready": False, "status": result.status}
