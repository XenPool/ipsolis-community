"""Celery Task: Install a PowerShell module from PSGallery.

Reads the desired module from the ps_modules DB table, runs
Install-Module -Scope CurrentUser (persisted via Docker volume),
and updates the status to installed / failed.
"""

import logging
import os
import subprocess

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tasks import app

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://xpuser:changeme@localhost:5432/itselfservice",
).replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _get_db_session() -> Session:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return Session(engine)


def _set_status(db: Session, ps_module_id: int, status: str, **kwargs) -> None:
    sets = ["status = :status", "updated_at = NOW()"]
    params: dict = {"id": ps_module_id, "status": status}
    for col, val in kwargs.items():
        sets.append(f"{col} = :{col}")
        params[col] = val
    db.execute(text(f"UPDATE ps_modules SET {', '.join(sets)} WHERE id = :id"), params)
    db.commit()


@app.task(
    name="tasks.workflows.ps_module_installer.install_ps_module",
    bind=True,
    queue="provision",
)
def install_ps_module(self, ps_module_id: int) -> dict:
    """Install or reinstall a PS module from PSGallery."""
    db = _get_db_session()
    try:
        row = db.execute(
            text("SELECT id, name, required_version FROM ps_modules WHERE id = :id"),
            {"id": ps_module_id},
        ).fetchone()

        if not row:
            return {"success": False, "error": f"ps_module id={ps_module_id} not found"}

        module_name = row.name
        required_version = row.required_version

        _set_status(db, ps_module_id, "installing", error_log=None, installed_version=None)
        logger.info("ps_module_installer: installing %s (version=%s)", module_name, required_version or "latest")

        # ── Install ────────────────────────────────────────────────────────────
        version_clause = (
            f"-RequiredVersion '{required_version}' " if required_version else ""
        )
        ps_cmd = (
            "Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; "
            f"Install-Module -Name '{module_name}' {version_clause}"
            "-Scope CurrentUser -Force -SkipPublisherCheck -AllowClobber"
        )

        result = subprocess.run(
            ["pwsh", "-NonInteractive", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "unknown error").strip()
            _set_status(db, ps_module_id, "failed", error_log=error[:4000])
            logger.error("ps_module_installer: failed %s: %s", module_name, error[:200])
            return {"success": False, "error": error}

        # ── Verify installation succeeded via Get-InstalledModule ──────────────
        # This is the authoritative check: if the module is not found locally
        # after a successful Install-Module call, PSGallery silently accepted an
        # unknown module name (e.g. due to API issues). Treat that as failure.
        ver_result = subprocess.run(
            [
                "pwsh", "-NonInteractive", "-NoProfile", "-Command",
                f"(Get-InstalledModule -Name '{module_name}' -ErrorAction SilentlyContinue).Version",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        installed_version = ver_result.stdout.strip()

        if not installed_version:
            error = (
                f"Module '{module_name}' was not installed. "
                "The module name may not exist in the PSGallery. "
                "Please verify the exact name at https://www.powershellgallery.com"
            )
            _set_status(db, ps_module_id, "failed", error_log=error)
            logger.warning("ps_module_installer: Get-InstalledModule returned nothing for %s", module_name)
            return {"success": False, "error": error}

        _set_status(db, ps_module_id, "installed", installed_version=installed_version)
        logger.info("ps_module_installer: installed %s=%s", module_name, installed_version)
        return {"success": True, "installed_version": installed_version}

    except subprocess.TimeoutExpired:
        _set_status(db, ps_module_id, "failed", error_log="Installation timed out after 600s")
        return {"success": False, "error": "timeout"}
    except Exception as exc:
        logger.exception("ps_module_installer: unexpected error for id=%s", ps_module_id)
        try:
            _set_status(db, ps_module_id, "failed", error_log=str(exc)[:4000])
        except Exception:
            pass
        return {"success": False, "error": str(exc)}
    finally:
        db.close()
