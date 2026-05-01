"""Admin-API: Maintenance (backups, cleanup, health, queue inspection).

All endpoints require X-Admin-Key or an authenticated admin session.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.db_backup import DbBackup
from app.utils.auth import require_admin_key
from app.utils.features import require_enterprise
from app.utils.rbac import require_role

# Backups + health probes + queue inspection + alert config are
# community-tier — they're operational hygiene every install needs.
# The *audit-retention policy* knob is the only Enterprise gate left
# in this router; per-classification PII/PHI/PCI windows are the
# compliance feature, while routine prune behaviour with the global
# default is community.
_ENT = Depends(lambda: None)               # legacy alias — kept for back-compat with any in-flight code
_ENT_RETENTION = require_enterprise("audit_retention")

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/maintenance",
    tags=["admin-maintenance"],
    # RBAC slice 4: relaxed router floor from ``admin`` to ``auditor`` so
    # read endpoints (list backups, view retention config, queue depth,
    # schedule, alerts, health) are visible to auditors for compliance
    # review. Every write/trigger route below adds an explicit
    # ``require_role("admin")`` so audit access stays read-only.
    # ``GET /backups/{id}/download`` keeps the admin gate — backup files
    # contain the full DB and aren't a "read" the way the listing is.
    dependencies=[Depends(require_admin_key), require_role("auditor")],
)

# Per-route write gate — keeps the slice-4 read relaxation from
# accidentally letting auditors trigger backups, purge queues, etc.
_WRITE_GATE = require_role("admin")

BACKUP_DIR = Path("/app/backups")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

_SAFE_NAME = re.compile(r"^xp_backup_\d{8}_\d{6}\.sql\.gz$")


def _get_celery():
    from celery import Celery
    return Celery(broker=settings.CELERY_BROKER_URL)


# ── Backups ───────────────────────────────────────────────────────────────────


class BackupCreate(BaseModel):
    note: str | None = None


def _session_user(request: Request) -> str | None:
    s = request.session
    return s.get("admin_email") or s.get("admin_user") or "admin"


@router.get("/backups")
async def list_backups(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(
        select(DbBackup).order_by(DbBackup.created_at.desc()).limit(200)
    )
    rows = result.scalars().all()
    out = []
    for b in rows:
        out.append({
            "id":          b.id,
            "filename":    b.filename,
            "size_bytes":  b.size_bytes,
            "status":      b.status,
            "trigger":     b.trigger,
            "created_by":  b.created_by,
            "note":        b.note,
            "error":       b.error,
            "created_at":  b.created_at.isoformat() if b.created_at else None,
            "finished_at": b.finished_at.isoformat() if b.finished_at else None,
        })
    return out


_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB — matches nginx client_max_body_size
_GZIP_MAGIC = b"\x1f\x8b"


def _safe_upload_filename(orig: str) -> str:
    """Derive a safe on-disk filename from the operator's upload.

    Strips any path components (defence against directory-traversal in
    the client-supplied name), keeps the basename, and prefixes a
    timestamp so two uploads with the same name don't clobber each
    other. The result always ends in ``.sql.gz``.
    """
    base = os.path.basename(orig or "").strip()
    # Reject empty or pathological names — fall back to a generic stem.
    if not base or base in (".", "..") or any(c in base for c in "/\\"):
        base = "upload.sql.gz"
    if not base.lower().endswith(".sql.gz"):
        # Attach the right extension rather than reject; some operators
        # transfer files via tools that strip / rename extensions.
        base = base + ".sql.gz" if not base.lower().endswith(".gz") else base.rsplit(".", 1)[0] + ".sql.gz"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Final shape: xp_backup_uploaded_<ts>_<original-base>.sql.gz
    # so it's still recognisable in `ls ./backups/` but doesn't collide.
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base[: -len(".sql.gz")])
    return f"xp_backup_uploaded_{ts}_{cleaned}.sql.gz"


@router.post("/backups/upload", dependencies=[_ENT, _WRITE_GATE])
async def upload_backup(
    request: Request,
    file: UploadFile = File(..., description="The .sql.gz dump previously downloaded from another instance."),
    note: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Accept a previously-downloaded .sql.gz dump and register it.

    The uploaded file is streamed to ``./backups/<safe>.part`` to keep
    memory bounded for large dumps, then atomically renamed once the
    transfer completes and validation passes. Validations:

    * Filename ends in ``.sql.gz`` (or is rewritten to)
    * Size cap (matches the nginx ``client_max_body_size``)
    * Gzip magic bytes (``1f 8b``) at offset 0

    On success a ``db_backups`` row is created with ``trigger='upload'``
    and ``status='success'`` so the file shows up in the standard list
    and can be restored via the normal Restore button.
    """
    if not file or not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No file provided.")

    safe_filename = _safe_upload_filename(file.filename)
    target = BACKUP_DIR / safe_filename
    tmp = target.with_suffix(target.suffix + ".part")

    # Stream the upload to ``.part``. Reading in 1 MiB chunks keeps the
    # api process from holding the whole upload in memory at once.
    chunk_size = 1024 * 1024
    total = 0
    head = b""
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                if not head:
                    # Capture the first few bytes for magic-number sniffing
                    # before committing to the file.
                    head = chunk[:4]
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"Upload exceeds {_MAX_UPLOAD_BYTES // (1024*1024)} MiB limit.",
                    )
                out.write(chunk)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        logger.exception("Backup upload write failed: %s", exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Failed to write upload to disk: {exc}",
        )

    # Validate AFTER the full transfer so the client gets one clean error
    # rather than mid-stream truncation.
    if total == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty.")
    if not head.startswith(_GZIP_MAGIC):
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "File is not gzip-compressed (missing magic bytes 1f 8b). "
            "Expected a .sql.gz dump produced by ip·Solis or `pg_dump | gzip`.",
        )

    # Atomic move into place. Same pattern as the worker's pg_dump path.
    try:
        tmp.rename(target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Could not finalize upload to {target}: {exc}",
        )

    # Register in db_backups so it appears in the listing and is
    # eligible for restore.
    backup = DbBackup(
        filename=safe_filename,
        status="success",                 # file is already complete + validated
        trigger="upload",
        created_by=_session_user(request),
        size_bytes=total,
        finished_at=datetime.now(timezone.utc),
        note=(note or f"Uploaded from {os.path.basename(file.filename)}")[:500],
    )
    db.add(backup)
    await db.commit()
    await db.refresh(backup)

    logger.info(
        "Backup uploaded: id=%s filename=%s size=%s by=%s",
        backup.id, safe_filename, total, _session_user(request),
    )
    return {
        "id":         backup.id,
        "filename":   safe_filename,
        "size_bytes": total,
        "status":     "success",
        "trigger":    "upload",
        "message":    "Upload accepted. Use the Restore button to apply it.",
    }


@router.post("/backups", dependencies=[_ENT, _WRITE_GATE])
async def create_backup(
    request: Request,
    payload: BackupCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Creates a pending db_backups row and enqueues the worker task."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"xp_backup_{ts}.sql.gz"

    created_by = _session_user(request)
    backup = DbBackup(
        filename=filename,
        status="pending",
        trigger="manual",
        created_by=created_by,
        note=(payload.note or None),
    )
    db.add(backup)
    await db.commit()
    await db.refresh(backup)

    celery = _get_celery()
    task = celery.send_task(
        "tasks.modules.maintenance.run_backup",
        args=[backup.id, "manual"],
        queue="default",
    )
    logger.info("Enqueued backup id=%s task=%s", backup.id, task.id)
    return {
        "id":          backup.id,
        "filename":    backup.filename,
        "status":      backup.status,
        "task_id":     task.id,
    }


@router.get("/backups/{backup_id}/download", dependencies=[_ENT, _WRITE_GATE])
async def download_backup(
    backup_id: int, db: AsyncSession = Depends(get_db)
) -> FileResponse:
    backup = await db.get(DbBackup, backup_id)
    if not backup:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backup not found")
    if backup.status != "success":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Backup is in status '{backup.status}' — cannot download",
        )
    if not _SAFE_NAME.match(backup.filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unexpected filename format")
    path = BACKUP_DIR / backup.filename
    if not path.exists():
        raise HTTPException(
            status.HTTP_410_GONE,
            "Backup file is missing on disk (was it deleted manually?)",
        )
    return FileResponse(
        path=str(path),
        media_type="application/gzip",
        filename=backup.filename,
    )


class RestoreRequest(BaseModel):
    """Body for ``POST /admin/maintenance/backups/{id}/restore``.

    ``confirm_filename`` must equal the backup's filename verbatim — a
    typed confirmation that defeats accidental clicks. Operators see
    the filename in the backups table; the UI's modal sends whatever
    they type back here.
    """
    confirm_filename: str = Field(min_length=1, max_length=255)


@router.post("/backups/{backup_id}/restore", dependencies=[_ENT, _WRITE_GATE])
async def restore_backup(
    backup_id: int,
    payload: RestoreRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Restore the DB from a previously-taken backup.

    Workflow (most logic lives in
    ``tasks.modules.maintenance.run_restore`` — the api here only
    validates the request, takes the safety-backup row reservation,
    and enqueues):

      1. Verify the typed confirmation matches the backup's filename.
      2. Refuse if any restore is currently in progress.
      3. Insert a "pre-restore" db_backups row reserved for the
         worker to fill — gives the operator a same-tick rollback
         path if the restored data turns out to be wrong.
      4. Mark the target backup row as ``restoring`` so the UI can
         poll status.
      5. Enqueue ``run_restore`` and return 202 with both ids.
    """
    backup = await db.get(DbBackup, backup_id)
    if not backup:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backup not found")
    if backup.status not in ("success",):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot restore from a backup with status {backup.status!r}; "
            "only completed backups (status='success') are valid sources.",
        )
    if not (BACKUP_DIR / backup.filename).exists():
        raise HTTPException(
            status.HTTP_410_GONE,
            f"Backup file {backup.filename} is no longer on disk — "
            "cannot restore.",
        )
    # Typed-filename confirmation. Any mismatch (including stray
    # whitespace) refuses with the same generic message so the UI
    # can show "type the filename exactly" without leaking which
    # part the operator got wrong.
    if payload.confirm_filename != backup.filename:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "confirm_filename does not match the backup filename — "
            "type it exactly to confirm.",
        )

    # Single-flight: refuse if any other backup is in mid-restore.
    in_flight = await db.execute(
        select(DbBackup).where(DbBackup.status == "restoring").limit(1)
    )
    if in_flight.scalar_one_or_none():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Another restore is already in progress; wait for it to finish.",
        )

    # Reserve the safety-backup row before kicking the worker.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safety = DbBackup(
        filename=f"xp_backup_pre_restore_{ts}.sql.gz",
        status="pending",
        trigger="pre_restore",
        created_by=_session_user(request),
        note=f"Auto-taken before restoring backup #{backup.id} ({backup.filename})",
    )
    db.add(safety)
    # Mark target as restoring atomically with the safety insertion.
    backup.status = "restoring"
    await db.commit()
    await db.refresh(safety)
    await db.refresh(backup)

    celery = _get_celery()
    task = celery.send_task(
        "tasks.modules.maintenance.run_restore",
        args=[backup.id, safety.id],
        queue="default",
    )
    logger.warning(
        "Enqueued RESTORE backup id=%s (safety id=%s) task=%s by=%s",
        backup.id, safety.id, task.id, _session_user(request),
    )
    return {
        "status":              202,
        "target_backup_id":    backup.id,
        "target_filename":     backup.filename,
        "safety_backup_id":    safety.id,
        "safety_filename":     safety.filename,
        "task_id":             task.id,
        "message": (
            "Restore enqueued. Poll the backups list and watch this row's "
            "status flip from 'restoring' → 'restored' (success) or "
            "'restore_failed' (failure). The pre-restore safety backup is "
            "available for rollback at any time."
        ),
    }


@router.delete("/backups/{backup_id}", dependencies=[_ENT, _WRITE_GATE])
async def delete_backup(
    backup_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    backup = await db.get(DbBackup, backup_id)
    if not backup:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Backup not found")
    if _SAFE_NAME.match(backup.filename):
        path = BACKUP_DIR / backup.filename
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not delete backup file %s: %s", path, exc)
    await db.delete(backup)
    await db.commit()
    return {"success": True, "id": backup_id}


# ── Retention ─────────────────────────────────────────────────────────────────


_RETENTION_KEYS = [
    # (table, config_key, timestamp_column)
    ("orders",                  "retention.orders_days",          "created_at"),
    ("audit_log",               "retention.audit_log_days",       "timestamp"),
    ("standalone_runbook_runs", "retention.standalone_runs_days", "created_at"),
]


class RetentionUpdate(BaseModel):
    orders_days: int | None = None
    audit_log_days: int | None = None
    standalone_runs_days: int | None = None
    keep_last_n_backups: int | None = None


@router.get("/retention")
async def get_retention(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await db.execute(text(
        "SELECT key, value FROM app_config WHERE key IN ("
        "'retention.orders_days', 'retention.audit_log_days', "
        "'retention.standalone_runs_days', 'backup.keep_last_n')"
    ))
    cfg = {k: v for k, v in rows.all()}
    out = {
        "orders_days":          int(cfg.get("retention.orders_days") or 0),
        "audit_log_days":       int(cfg.get("retention.audit_log_days") or 0),
        "standalone_runs_days": int(cfg.get("retention.standalone_runs_days") or 0),
        "keep_last_n_backups":  int(cfg.get("backup.keep_last_n") or 0),
        "tables": [],
    }
    # Row counts + oldest/newest for each managed table
    for table, _key, ts_col in _RETENTION_KEYS:
        row = await db.execute(text(
            f"SELECT COUNT(*), MIN({ts_col}), MAX({ts_col}) FROM {table}"
        ))
        n, oldest, newest = row.first()
        out["tables"].append({
            "table":  table,
            "count":  int(n or 0),
            "oldest": oldest.isoformat() if oldest else None,
            "newest": newest.isoformat() if newest else None,
        })
    return out


@router.get("/beat-schedule")
async def list_beat_schedule() -> list[dict]:
    """Return the static catalog of every Beat-scheduled task.

    Sourced from ``app.utils.beat_inventory.BEAT_INVENTORY`` — a
    hand-maintained mirror of the worker's ``beat_schedule`` dict so
    the api ↔ worker code stays decoupled. The page that consumes
    this is read-only: operators get a "what runs when" overview
    plus links to the relevant config keys, but can't reschedule
    from here (footgun avoidance — see notes in beat_inventory.py).
    """
    from app.utils.beat_inventory import BEAT_INVENTORY
    # Return a list of plain dicts (TypedDict instances are dicts at
    # runtime, but FastAPI's response serialiser is happier with this
    # explicit list comprehension).
    return [dict(entry) for entry in BEAT_INVENTORY]


@router.put("/retention", dependencies=[_ENT_RETENTION, _WRITE_GATE])
async def set_retention(
    payload: RetentionUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    updates = {
        "retention.orders_days":          payload.orders_days,
        "retention.audit_log_days":       payload.audit_log_days,
        "retention.standalone_runs_days": payload.standalone_runs_days,
        "backup.keep_last_n":             payload.keep_last_n_backups,
    }
    for key, value in updates.items():
        if value is None:
            continue
        if value < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be >= 0")
        await db.execute(
            text(
                "INSERT INTO app_config (key, value, description, is_secret) "
                "VALUES (:k, :v, NULL, false) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
            ),
            {"k": key, "v": str(value)},
        )
    await db.commit()
    return {"success": True}


@router.post("/cleanup", dependencies=[_WRITE_GATE])
async def run_cleanup(
    dry_run: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Preview or execute retention cleanup, inline.

    Both dry-run and the actual delete run directly in the API's async
    session. The queries are simple COUNT/DELETE statements against indexed
    timestamp columns — fast enough that a Celery round-trip would only add
    failure modes (tasks stuck in a backlogged queue).
    """
    cfg_rows = (
        await db.execute(
            text(
                "SELECT key, value FROM app_config WHERE key IN "
                "('retention.orders_days', 'retention.audit_log_days', "
                "'retention.standalone_runs_days')"
            )
        )
    ).fetchall()
    cfg = {r[0]: r[1] for r in cfg_rows}
    summary: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    for table, key, col in _RETENTION_KEYS:
        raw = (cfg.get(key) or "").strip()
        days = int(raw) if raw.isdigit() else 0
        if days <= 0:
            summary[table] = {"days": days, "skipped": True}
            continue
        cutoff = now - timedelta(days=days)
        count_row = (
            await db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {col} < :c"),  # noqa: S608 — table is from the fixed _RETENTION_KEYS list, not user input
                {"c": cutoff},
            )
        ).first()
        n = int(count_row[0]) if count_row else 0
        if dry_run:
            summary[table] = {"days": days, "would_delete": n}
        else:
            await db.execute(
                text(f"DELETE FROM {table} WHERE {col} < :c"),  # noqa: S608 — table is from the fixed _RETENTION_KEYS list, not user input
                {"c": cutoff},
            )
            summary[table] = {"days": days, "deleted": n}
    if not dry_run:
        await db.commit()
        logger.info("admin: retention cleanup deleted rows per table: %s", summary)
    return {"enqueued": False, "success": True, "dry_run": dry_run, "summary": summary}


# ── Health probes ─────────────────────────────────────────────────────────────


async def _probe_db(db: AsyncSession) -> dict:
    try:
        res = await db.execute(text("SELECT version()"))
        version = (res.first() or ("?",))[0]
        return {"ok": True, "detail": str(version)[:120]}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}


def _probe_redis() -> dict:
    try:
        import redis  # type: ignore[import-not-found]
    except Exception as exc:
        return {"ok": False, "detail": f"redis package missing: {exc}"}
    try:
        url = settings.CELERY_BROKER_URL
        r = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        r.ping()
        info = r.info(section="server")
        return {"ok": True, "detail": f"redis {info.get('redis_version', '?')}"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}


async def _probe_entra(db: AsyncSession) -> dict:
    row = await db.execute(
        text("SELECT value FROM app_config WHERE key = 'entra.mode'")
    )
    mode = (row.first() or (None,))[0]
    if (mode or "disabled") == "disabled":
        return {"ok": None, "detail": "disabled"}
    try:
        from app.utils.entra import _get_entra_config, get_msal_app
        cfg = await _get_entra_config(db)
        msal_app = get_msal_app(cfg)
        if msal_app is None:
            return {"ok": False, "detail": "Missing tenant_id, client_id, or client_secret"}
        result = msal_app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" in result:
            return {"ok": True, "detail": f"token acquired (mode={mode})"}
        err = result.get("error_description") or result.get("error") or "unknown error"
        return {"ok": False, "detail": str(err)[:200]}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}


async def _probe_sccm(db: AsyncSession) -> dict:
    row = await db.execute(text(
        "SELECT key, value FROM app_config WHERE key IN "
        "('sccm.base_url', 'sccm.username', 'sccm.realm', 'sccm.kdc')"
    ))
    cfg = {k: v for k, v in row.all()}
    base = (cfg.get("sccm.base_url") or "").strip()
    if not base:
        return {"ok": None, "detail": "not configured"}
    # Enqueue the existing sccm_probe (pwsh+Kerberos) task and wait briefly
    try:
        import asyncio
        from celery import Celery
        client = Celery(broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND)
        def _probe() -> dict:
            try:
                ar = client.send_task("tasks.workflows.sccm_probe.probe", queue="provision")
                return ar.get(timeout=20)
            except Exception as exc:
                return {"ok": False, "message": str(exc)}
        result = await asyncio.get_running_loop().run_in_executor(None, _probe)
        ok = result.get("ok")
        detail = result.get("message") or base
        return {"ok": bool(ok) if ok is not None else None, "detail": str(detail)[:200]}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)[:200]}


async def _probe_smtp(db: AsyncSession) -> dict:
    import smtplib
    import socket as _socket
    row = await db.execute(text(
        "SELECT key, value FROM app_config "
        "WHERE key IN ('email.smtp_server', 'email.smtp_port')"
    ))
    cfg = {k: v for k, v in row.all()}
    host = (cfg.get("email.smtp_server") or "").strip()
    port_s = (cfg.get("email.smtp_port") or "25").strip()
    if not host:
        return {"ok": None, "detail": "not configured"}
    try:
        port = int(port_s) if port_s.isdigit() else 25
    except Exception:
        port = 25
    try:
        with smtplib.SMTP(host, port, timeout=4) as s:
            s.ehlo()
        return {"ok": True, "detail": f"connected to {host}:{port}"}
    except (OSError, _socket.timeout, smtplib.SMTPException) as exc:
        return {"ok": False, "detail": str(exc)[:200]}


# RedBeat key prefix mirrors ``worker/tasks/__init__.py``. The lock key
# the leader holds lives at ``<prefix>:lock`` — note the double colon
# (the prefix already ends in one). Keep this in lockstep with the worker.
_REDBEAT_LOCK_KEY = "ipsolis:redbeat::lock"


def _probe_beat() -> dict:
    """Check whether a Beat replica holds the RedBeat distributed lock.

    A present lock means a Beat replica is dispatching scheduled tasks.
    A missing lock for >30 s (the configured ``redbeat_lock_timeout``)
    means **no** Beat is running and periodic tasks are silently
    stalled — exactly the failure mode that prompts the alert.

    Returns the tri-state {True, False, None} ``ok`` field consistent
    with the other probes:
    * ``True``  — lock present, Beat alive somewhere
    * ``False`` — Redis reachable but lock missing → real outage
    * ``None``  — Redis unreachable; redirect attention to the redis probe
    """
    try:
        import redis  # type: ignore[import-not-found]
    except Exception as exc:
        return {"ok": False, "detail": f"redis package missing: {exc}"}
    try:
        r = redis.Redis.from_url(settings.CELERY_BROKER_URL,
                                 socket_connect_timeout=2, socket_timeout=2)
        if not r.exists(_REDBEAT_LOCK_KEY):
            return {
                "ok": False,
                "detail": (
                    "RedBeat lock key is absent — no Beat replica is dispatching "
                    "scheduled tasks. Periodic jobs (reminders, snapshots, "
                    "retention prune, license expiry, threshold alerter) are "
                    "stalled until a Beat replica recovers."
                ),
            }
        ttl = r.ttl(_REDBEAT_LOCK_KEY)
        return {"ok": True, "detail": f"Beat leader present (lock TTL ~{ttl}s)"}
    except Exception as exc:
        # Redis itself is unreachable — covered by ``redis`` probe.
        return {"ok": None, "detail": f"redis unreachable: {str(exc)[:160]}"}


# Note: SIEM streaming probe lives next to the ``/health`` handler
# below (``_probe_siem_streaming_async``). Keeping it co-located with
# the response builder makes the data-flow easier to read for a route
# that's pure orchestration.


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    siem = await _probe_siem_streaming_async(db)
    return {
        "database": await _probe_db(db),
        "redis":    _probe_redis(),
        "beat":     _probe_beat(),
        "entra":    await _probe_entra(db),
        "sccm":     await _probe_sccm(db),
        "smtp":     await _probe_smtp(db),
        "siem":     siem,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def _probe_siem_streaming_async(db: AsyncSession) -> dict:
    """Async wrapper around the sync SIEM probe.

    Cleaner than spinning up a sync DB session in this path — we already
    have an async session, so just translate the SELECT directly.
    """
    rows = await db.execute(
        text(
            "SELECT key, value FROM app_config "
            "WHERE key IN ('siem.enabled','siem.last_error','siem.last_success_at')"
        )
    )
    cfg = {k: (v or "") for k, v in rows.all()}
    enabled = (cfg.get("siem.enabled", "")).strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        return {"ok": None, "detail": "SIEM streaming disabled"}
    last_error = (cfg.get("siem.last_error") or "").strip()
    if last_error:
        return {"ok": False, "detail": f"streaming failure: {last_error[:180]}"}
    last_success = (cfg.get("siem.last_success_at") or "").strip()
    if last_success:
        return {"ok": True, "detail": f"last successful batch at {last_success}"}
    return {"ok": True, "detail": "enabled, awaiting first successful batch"}


# ── Queue inspection ──────────────────────────────────────────────────────────


_KNOWN_QUEUES = ("default", "provision", "reclaim", "notifications")


@router.get("/queue")
async def queue_status() -> dict:
    """Returns queue depth (Redis LLEN) + worker-side active/reserved task counts."""
    # Queue depth via redis
    depths: dict[str, int | str] = {}
    try:
        import redis  # type: ignore[import-not-found]
        r = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2)
        for q in _KNOWN_QUEUES:
            try:
                depths[q] = int(r.llen(q))
            except Exception as exc:
                depths[q] = f"err: {exc}"
    except Exception as exc:
        depths = {"error": str(exc)}

    # Worker activity via celery control
    workers: dict[str, dict] = {}
    try:
        celery = _get_celery()
        insp = celery.control.inspect(timeout=2.0)
        active = insp.active() or {}
        reserved = insp.reserved() or {}
        ping = insp.ping() or {}
        for name, status_dict in ping.items():
            workers[name] = {
                "ok":       (status_dict or {}).get("ok") == "pong",
                "active":   len(active.get(name, [])),
                "reserved": len(reserved.get(name, [])),
            }
    except Exception as exc:
        workers = {"error": str(exc)}

    return {"queues": depths, "workers": workers}


class QueuePurge(BaseModel):
    queue: str


@router.post("/queue/purge", dependencies=[_ENT, _WRITE_GATE])
async def purge_queue(payload: QueuePurge) -> dict:
    if payload.queue not in _KNOWN_QUEUES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown queue: {payload.queue}")
    try:
        import redis  # type: ignore[import-not-found]
        r = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2)
        n = int(r.llen(payload.queue))
        r.delete(payload.queue)
        return {"success": True, "queue": payload.queue, "removed": n}
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


# ── Backup schedule ───────────────────────────────────────────────────────────


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    cron: str | None = None


def _validate_cron(expr: str) -> None:
    try:
        from croniter import croniter  # type: ignore[import-not-found]
    except Exception:
        # croniter is only in the worker image; accept unvalidated in api if missing
        return
    if not croniter.is_valid(expr):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid cron expression: {expr!r}")


async def _upsert_cfg(db: AsyncSession, key: str, value: str) -> None:
    await db.execute(
        text(
            "INSERT INTO app_config (key, value, description, is_secret) "
            "VALUES (:k, :v, NULL, false) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        ),
        {"k": key, "v": value},
    )


@router.get("/schedule", dependencies=[_ENT])
async def get_schedule(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await db.execute(text(
        "SELECT key, value FROM app_config "
        "WHERE key IN ('backup.enabled', 'backup.schedule_cron')"
    ))
    cfg = {k: v for k, v in rows.all()}
    return {
        "enabled": (cfg.get("backup.enabled") or "false").lower() in ("1", "true", "yes", "on"),
        "cron":    cfg.get("backup.schedule_cron") or "0 2 * * *",
    }


@router.put("/schedule", dependencies=[_ENT, _WRITE_GATE])
async def set_schedule(
    payload: ScheduleUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    if payload.cron is not None:
        cron = payload.cron.strip()
        if not cron:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cron must not be empty")
        _validate_cron(cron)
        await _upsert_cfg(db, "backup.schedule_cron", cron)
    if payload.enabled is not None:
        await _upsert_cfg(db, "backup.enabled", "true" if payload.enabled else "false")
    await db.commit()
    return {"success": True}


# ── Health alerts ─────────────────────────────────────────────────────────────


class AlertUpdate(BaseModel):
    enabled: bool | None = None
    email: str | None = None
    cooldown_minutes: int | None = None


@router.get("/alerts", dependencies=[_ENT])
async def get_alerts(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await db.execute(text(
        "SELECT key, value FROM app_config WHERE key IN ("
        "'health.alert_enabled', 'health.alert_email', 'health.alert_cooldown_minutes')"
    ))
    cfg = {k: v for k, v in rows.all()}
    cooldown = cfg.get("health.alert_cooldown_minutes") or "60"
    return {
        "enabled":          (cfg.get("health.alert_enabled") or "false").lower() in ("1", "true", "yes", "on"),
        "email":            cfg.get("health.alert_email") or "",
        "cooldown_minutes": int(cooldown) if cooldown.isdigit() else 60,
    }


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.put("/alerts", dependencies=[_ENT, _WRITE_GATE])
async def set_alerts(
    payload: AlertUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    if payload.email is not None:
        email = payload.email.strip()
        if email and not _EMAIL_RE.match(email):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid email: {email!r}")
        await _upsert_cfg(db, "health.alert_email", email)
    if payload.cooldown_minutes is not None:
        if payload.cooldown_minutes < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cooldown_minutes must be >= 0")
        await _upsert_cfg(db, "health.alert_cooldown_minutes", str(payload.cooldown_minutes))
    if payload.enabled is not None:
        await _upsert_cfg(db, "health.alert_enabled", "true" if payload.enabled else "false")
    await db.commit()
    return {"success": True}


@router.post("/alerts/test", dependencies=[_ENT, _WRITE_GATE])
async def test_alert(db: AsyncSession = Depends(get_db)) -> dict:
    """Sends a test email to the configured alert recipient."""
    row = await db.execute(
        text("SELECT value FROM app_config WHERE key = 'health.alert_email'")
    )
    to_addr = ((row.first() or ("",))[0] or "").strip()
    if not to_addr:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No alert recipient configured")
    celery = _get_celery()
    result = celery.send_task(
        "tasks.modules.maintenance.send_test_alert_email",
        queue="default",
    )
    return {"enqueued": True, "task_id": result.id, "recipient": to_addr}
