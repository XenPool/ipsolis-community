"""Maintenance – DB backup, retention cleanup, and service health probes.

Celery tasks exposed under `tasks.modules.maintenance.*`:
  - run_backup(backup_id, trigger)          # invoked by API / scheduler
  - run_cleanup(dry_run)                    # invoked by API or Beat schedule
  - check_backup_schedule()                 # Beat every minute — dispatches scheduled backups
  - check_health_and_alert()                # Beat every 5 min — emails on probe state changes

Backup files live under /app/backups/ (shared volume between api + worker).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from celery import shared_task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

BACKUP_DIR = Path("/app/backups")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

_FILENAME_RE = re.compile(r"^xp_backup_\d{8}_\d{6}\.sql\.gz$")


# ── DB session helper (sync) ──────────────────────────────────────────────────


def _sync_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    # Worker already uses psycopg2, but if called with asyncpg URL convert it
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)


_engine = create_engine(_sync_url(), pool_pre_ping=True, pool_size=2, max_overflow=2)
SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def _db() -> Session:
    return SessionLocal()


# ── Backups ───────────────────────────────────────────────────────────────────


def _parse_pg_url() -> dict[str, str]:
    """Parses DATABASE_URL for pg_dump invocation."""
    raw = os.environ.get("DATABASE_URL", "")
    # Strip SQLAlchemy driver prefix if present
    if raw.startswith("postgresql+"):
        raw = "postgresql://" + raw.split("://", 1)[1]
    p = urlparse(raw)
    return {
        "host":     p.hostname or "postgres",
        "port":     str(p.port or 5432),
        "user":     p.username or "",
        "password": p.password or "",
        "dbname":   (p.path or "/").lstrip("/"),
    }


def _enforce_keep_last_n(db: Session) -> None:
    """Delete backup files and rows beyond the configured retention count."""
    row = db.execute(
        text("SELECT value FROM app_config WHERE key = 'backup.keep_last_n'")
    ).first()
    keep = int(row[0]) if row and row[0] and row[0].isdigit() else 0
    if keep <= 0:
        return
    rows = db.execute(
        text(
            "SELECT id, filename FROM db_backups "
            "WHERE status = 'success' ORDER BY id DESC OFFSET :k"
        ),
        {"k": keep},
    ).fetchall()
    for row in rows:
        bid, filename = row[0], row[1]
        try:
            (BACKUP_DIR / filename).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not unlink backup %s: %s", filename, exc)
        db.execute(text("DELETE FROM db_backups WHERE id = :i"), {"i": bid})
    db.commit()


@shared_task(name="tasks.modules.maintenance.run_backup", bind=True)
def run_backup(self, backup_id: int, trigger: str = "manual") -> dict:
    """Runs pg_dump against DATABASE_URL, stores the file under /app/backups/.

    The db_backups row is expected to exist already (status='pending') —
    the API endpoint inserts it before enqueuing this task so the UI can
    show the pending record immediately.
    """
    db = _db()
    try:
        row = db.execute(
            text("SELECT filename FROM db_backups WHERE id = :i"),
            {"i": backup_id},
        ).first()
        if not row:
            logger.error("run_backup: no db_backups row with id=%s", backup_id)
            return {"success": False, "error": "backup row not found"}
        filename = row[0]
        target = BACKUP_DIR / filename

        db.execute(
            text(
                "UPDATE db_backups SET status='running', trigger=:t "
                "WHERE id = :i"
            ),
            {"i": backup_id, "t": trigger},
        )
        db.commit()

        pg = _parse_pg_url()
        env = os.environ.copy()
        if pg["password"]:
            env["PGPASSWORD"] = pg["password"]

        # pg_dump custom format + gzip for smaller file + atomic write (.part)
        tmp = target.with_suffix(target.suffix + ".part")
        cmd = [
            "pg_dump",
            "--host",     pg["host"],
            "--port",     pg["port"],
            "--username", pg["user"],
            "--dbname",   pg["dbname"],
            "--format",   "plain",
            "--no-owner",
            "--no-privileges",
        ]
        with open(tmp, "wb") as out:
            dump = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            gzip_p = subprocess.Popen(["gzip", "-c"], stdin=dump.stdout, stdout=out)
            dump.stdout.close()
            _, dump_err = dump.communicate(timeout=3600)
            gzip_p.communicate(timeout=60)
            if dump.returncode != 0:
                raise RuntimeError(
                    "pg_dump failed: " + (dump_err.decode("utf-8", errors="replace")[:2000])
                )
            if gzip_p.returncode != 0:
                raise RuntimeError("gzip failed")

        tmp.rename(target)
        size = target.stat().st_size

        db.execute(
            text(
                "UPDATE db_backups SET status='success', size_bytes=:s, "
                "finished_at=NOW(), error=NULL WHERE id = :i"
            ),
            {"i": backup_id, "s": size},
        )
        db.commit()

        _enforce_keep_last_n(db)

        logger.info("Backup %s completed (%s bytes)", filename, size)
        return {"success": True, "backup_id": backup_id, "filename": filename, "size_bytes": size}

    except Exception as exc:
        logger.exception("Backup failed for id=%s: %s", backup_id, exc)
        try:
            db.execute(
                text(
                    "UPDATE db_backups SET status='failed', finished_at=NOW(), "
                    "error=:e WHERE id = :i"
                ),
                {"i": backup_id, "e": str(exc)[:4000]},
            )
            db.commit()
        except Exception:
            pass
        return {"success": False, "error": str(exc)}
    finally:
        db.close()


# ── Retention cleanup ─────────────────────────────────────────────────────────


_RETENTION_TABLES = [
    # (config_key, table_name, timestamp_column)
    ("retention.orders_days",           "orders",                  "created_at"),
    ("retention.audit_log_days",        "audit_log",               "timestamp"),
    ("retention.standalone_runs_days",  "standalone_runbook_runs", "created_at"),
]


def _get_retention(db: Session, key: str) -> int:
    row = db.execute(
        text("SELECT value FROM app_config WHERE key = :k"), {"k": key}
    ).first()
    if not row or not row[0] or not row[0].isdigit():
        return 0
    return int(row[0])


@shared_task(name="tasks.modules.maintenance.run_cleanup", bind=True)
def run_cleanup(self, dry_run: bool = False) -> dict:
    """Deletes rows older than the per-table retention window.

    Returns a summary per table: {orders: {days, would_delete|deleted}, ...}
    """
    db = _db()
    summary: dict[str, dict] = {}
    try:
        for key, table, col in _RETENTION_TABLES:
            days = _get_retention(db, key)
            if days <= 0:
                summary[table] = {"days": days, "skipped": True}
                continue
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            count_row = db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {col} < :c"),
                {"c": cutoff},
            ).first()
            n = int(count_row[0]) if count_row else 0
            if dry_run:
                summary[table] = {"days": days, "would_delete": n}
            else:
                db.execute(
                    text(f"DELETE FROM {table} WHERE {col} < :c"),
                    {"c": cutoff},
                )
                db.commit()
                summary[table] = {"days": days, "deleted": n}
        return {"success": True, "dry_run": dry_run, "summary": summary}
    except Exception as exc:
        logger.exception("Cleanup failed: %s", exc)
        db.rollback()
        return {"success": False, "error": str(exc), "summary": summary}
    finally:
        db.close()


# ── Scheduled backups (Beat) ──────────────────────────────────────────────────


def _bool_cfg(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


@shared_task(name="tasks.modules.maintenance.check_backup_schedule", bind=True)
def check_backup_schedule(self) -> dict:
    """Beat every minute – enqueues a scheduled backup when the cron fires."""
    try:
        from croniter import croniter
    except Exception as exc:
        logger.warning("croniter unavailable: %s", exc)
        return {"success": False, "error": "croniter missing"}

    db = _db()
    try:
        enabled_row = db.execute(
            text("SELECT value FROM app_config WHERE key = 'backup.enabled'")
        ).first()
        if not _bool_cfg(enabled_row[0] if enabled_row else None):
            return {"success": True, "skipped": "disabled"}

        cron_row = db.execute(
            text("SELECT value FROM app_config WHERE key = 'backup.schedule_cron'")
        ).first()
        cron_expr = (cron_row[0] if cron_row else "0 2 * * *") or "0 2 * * *"

        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        try:
            itr = croniter(cron_expr, now)
            prev_fire = itr.get_prev(datetime)
        except Exception as exc:
            logger.warning("Invalid backup.schedule_cron=%r: %s", cron_expr, exc)
            return {"success": False, "error": f"invalid cron: {exc}"}

        if prev_fire.tzinfo is None:
            prev_fire = prev_fire.replace(tzinfo=timezone.utc)

        if (now - prev_fire).total_seconds() > 60:
            return {"success": True, "skipped": "not-due", "prev_fire": prev_fire.isoformat()}

        # Dedupe: don't enqueue twice in the same minute
        dup = db.execute(
            text(
                "SELECT id FROM db_backups "
                "WHERE trigger = 'scheduled' AND created_at >= :m "
                "LIMIT 1"
            ),
            {"m": now},
        ).first()
        if dup:
            return {"success": True, "skipped": "already-enqueued", "backup_id": dup[0]}

        filename = f"xp_backup_{now.strftime('%Y%m%d_%H%M%S')}.sql.gz"
        ins = db.execute(
            text(
                "INSERT INTO db_backups (filename, status, trigger, created_by, created_at) "
                "VALUES (:f, 'pending', 'scheduled', 'beat', NOW()) RETURNING id"
            ),
            {"f": filename},
        ).first()
        backup_id = int(ins[0])
        db.commit()

        from tasks import app as celery_app
        celery_app.send_task(
            "tasks.modules.maintenance.run_backup",
            kwargs={"backup_id": backup_id, "trigger": "scheduled"},
            queue="default",
        )
        logger.info("Scheduled backup enqueued id=%s filename=%s", backup_id, filename)
        return {"success": True, "backup_id": backup_id, "filename": filename}
    except Exception as exc:
        logger.exception("check_backup_schedule failed: %s", exc)
        db.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        db.close()


# ── Health probe alerts (Beat) ────────────────────────────────────────────────


def _fetch_health() -> dict:
    """Calls the api's health endpoint using ADMIN_API_KEY. Returns parsed JSON."""
    base = os.environ.get("API_INTERNAL_URL", "http://api:8000")
    admin_key = os.environ.get("ADMIN_API_KEY", "")
    req = urllib.request.Request(
        f"{base}/admin/maintenance/health",
        headers={"X-Admin-Key": admin_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _send_health_email(db: Session, to_addr: str, subject: str, html_body: str) -> dict:
    from tasks.modules.config_reader import get_config
    from tasks.modules.notifications import MAIL_FROM, _production_send_html_email
    mail_from = get_config(db, "email.from_address", MAIL_FROM) or MAIL_FROM
    return _production_send_html_email(db, [to_addr], None, mail_from, subject, html_body)


@shared_task(name="tasks.modules.maintenance.send_test_alert_email", bind=True)
def send_test_alert_email(self) -> dict:
    """Sends a test email to the configured health.alert_email."""
    db = _db()
    try:
        row = db.execute(
            text("SELECT value FROM app_config WHERE key = 'health.alert_email'")
        ).first()
        to_addr = ((row[0] if row else "") or "").strip()
        if not to_addr:
            return {"success": False, "error": "no recipient configured"}
        now = datetime.now(timezone.utc).isoformat()
        body = (
            "<p>This is a test alert from ip·Solis.</p>"
            f"<p><strong>Time (UTC):</strong> {now}</p>"
            "<p>If you received this email, health-alert delivery is working.</p>"
        )
        res = _send_health_email(db, to_addr, "[XenPool] Health alert – test", body)
        return {"success": bool(res.get("success")), "recipient": to_addr, **res}
    finally:
        db.close()


@shared_task(name="tasks.modules.maintenance.check_health_and_alert", bind=True)
def check_health_and_alert(self) -> dict:
    """Beat every 5 min – emails when a probe flips state (OK↔FAIL).

    Compares the current health snapshot against the JSON in
    app_config.health.last_state and emits an email on transitions,
    respecting health.alert_cooldown_minutes to suppress repeat failures.
    """
    db = _db()
    try:
        enabled_row = db.execute(
            text("SELECT value FROM app_config WHERE key = 'health.alert_enabled'")
        ).first()
        if not _bool_cfg(enabled_row[0] if enabled_row else None):
            return {"success": True, "skipped": "disabled"}

        email_row = db.execute(
            text("SELECT value FROM app_config WHERE key = 'health.alert_email'")
        ).first()
        to_addr = (email_row[0] if email_row else "") or ""
        if not to_addr.strip():
            return {"success": True, "skipped": "no-recipient"}

        cooldown_row = db.execute(
            text("SELECT value FROM app_config WHERE key = 'health.alert_cooldown_minutes'")
        ).first()
        cooldown_min = int(cooldown_row[0]) if (cooldown_row and (cooldown_row[0] or "").isdigit()) else 60

        try:
            health = _fetch_health()
        except Exception as exc:
            logger.warning("Health fetch failed: %s", exc)
            return {"success": False, "error": f"fetch failed: {exc}"}

        services = health.get("services") or {}
        if not services:
            return {"success": True, "skipped": "no-services"}

        state_row = db.execute(
            text("SELECT value FROM app_config WHERE key = 'health.last_state'")
        ).first()
        try:
            last_state = json.loads(state_row[0]) if state_row and state_row[0] else {}
        except Exception:
            last_state = {}

        now = datetime.now(timezone.utc)
        new_state: dict = {}
        alerts_sent: list[str] = []

        for name, info in services.items():
            current_ok = bool(info.get("ok"))
            prev = last_state.get(name) or {}
            prev_ok = prev.get("ok")
            last_alert_at = prev.get("last_alert_at")
            last_alert_dt = None
            if last_alert_at:
                try:
                    last_alert_dt = datetime.fromisoformat(last_alert_at)
                    if last_alert_dt.tzinfo is None:
                        last_alert_dt = last_alert_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    last_alert_dt = None

            entry = {"ok": current_ok, "last_alert_at": last_alert_at}

            should_alert = False
            kind = None
            if prev_ok is None:
                # First observation: alert only if failing
                if not current_ok:
                    should_alert = True
                    kind = "failure"
            elif prev_ok and not current_ok:
                should_alert = True
                kind = "failure"
            elif not prev_ok and current_ok:
                should_alert = True
                kind = "recovery"
            elif not current_ok and not prev_ok:
                # Still failing – respect cooldown
                if last_alert_dt is None or (now - last_alert_dt).total_seconds() >= cooldown_min * 60:
                    should_alert = True
                    kind = "failure"

            if should_alert:
                detail = info.get("detail") or info.get("error") or ""
                status = "RECOVERED" if kind == "recovery" else "FAILED"
                subject = f"[XenPool] Health {status}: {name}"
                body = (
                    f"<p><strong>Service:</strong> {name}</p>"
                    f"<p><strong>Status:</strong> {'OK' if current_ok else 'FAILED'}</p>"
                    f"<p><strong>Detail:</strong> {detail}</p>"
                    f"<p><strong>Time (UTC):</strong> {now.isoformat()}</p>"
                )
                res = _send_health_email(db, to_addr.strip(), subject, body)
                if res.get("success"):
                    entry["last_alert_at"] = now.isoformat()
                    alerts_sent.append(f"{name}:{kind}")
                else:
                    logger.warning("Health alert email failed for %s: %s", name, res.get("error"))

            new_state[name] = entry

        db.execute(
            text(
                "UPDATE app_config SET value = :v WHERE key = 'health.last_state'"
            ),
            {"v": json.dumps(new_state)},
        )
        db.commit()

        return {"success": True, "alerts": alerts_sent, "services": list(services.keys())}
    except Exception as exc:
        logger.exception("check_health_and_alert failed: %s", exc)
        db.rollback()
        return {"success": False, "error": str(exc)}
    finally:
        db.close()
