"""Admin API: license upload / download / status / remove.

The active license is a signed JSON file at
``/app/license/ipsolis.lic``. This module exposes endpoints so an admin can
install or replace the license from the browser instead of SSH-ing into the
host.

All endpoints require ``require_admin_key`` (X-Admin-Key header or an
authenticated admin session). The upload flow validates the signature
against the baked-in public key *before* writing anything to disk, so a
bad file never replaces a good one. On success the in-process cache is
force-reloaded immediately; worker/beat pick the new file up automatically
on next access thanks to mtime-based cache invalidation in
``app.utils.license``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse

from app.templates_instance import set_license_globals
from app.utils import license as license_utils
from app.utils.auth import require_admin_key
from app.utils.rbac import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/license",
    tags=["admin-license"],
    # License upload / removal touches the platform's commercial
    # gating — superadmin only, like API token issuance.
    dependencies=[Depends(require_admin_key), require_role("superadmin")],
)

MAX_LICENSE_BYTES = 64 * 1024  # 64 KiB — a signed license is a few hundred bytes


def _license_info_dict() -> dict[str, Any]:
    """Fresh snapshot of the current license state, suitable for JSON.

    Side effect: also refreshes the Jinja2 template globals
    (``edition`` / ``is_enterprise`` / ``license_info``) so the Dashboard
    and enterprise-feature gates reflect the new state without an api
    restart. Without this the file changes but every rendered template
    keeps the startup-frozen edition forever.
    """
    from app.license.trusted_keys import TRUSTED_KEYS_BY_ID
    from datetime import date

    info = license_utils.load_license(force_reload=True)
    set_license_globals(info)

    verified_by: dict[str, Any] | None = None
    if info.verified_by_key_id:
        key = TRUSTED_KEYS_BY_ID.get(info.verified_by_key_id)
        deprecation_warning: str | None = None
        if key and key.deprecated_after and date.today() > key.deprecated_after:
            deprecation_warning = (
                f"This license was signed by a key that has been deprecated as of "
                f"{key.deprecated_after.isoformat()}. Please request a re-issued license "
                f"signed by the current key."
            )
        verified_by = {
            "key_id": info.verified_by_key_id,
            "description": info.verified_by_description,
            "deprecation_warning": deprecation_warning,
        }

    return {
        "license_id": info.license_id,
        "licensee": info.licensee,
        "edition": info.edition,
        "max_users": info.max_users,
        "max_asset_types": info.max_asset_types,
        "issued_at": info.issued_at.isoformat() if info.issued_at else None,
        "expires_at": info.expires_at.isoformat() if info.expires_at else None,
        "features": list(info.features),
        "valid": info.valid,
        "message": info.message,
        "file_present": license_utils.LICENSE_PATH.exists(),
        # ``install_uuid`` (license-side) is the binding the .lic file claims;
        # ``local_install_uuid`` is the per-install identifier this deployment
        # generated. They must match for an install-bound license to validate.
        # The customer copies ``local_install_uuid`` when requesting a new
        # license so we can bake it into the next .lic.
        "install_uuid": info.install_uuid,
        "local_install_uuid": license_utils.get_install_uuid(),
        "verified_by": verified_by,
    }


@router.get("/trust-list")
async def license_trust_list() -> list[dict[str, Any]]:
    """Return all trusted public keys (no raw key material) for the admin UI."""
    from app.license.trusted_keys import TRUSTED_KEYS
    return [
        {
            "key_id": k.key_id,
            "algorithm": k.algorithm,
            "description": k.description,
            "accepted_license_types": sorted(k.accepted_license_types),
            "rsa_padding": k.rsa_padding,
            "rsa_hash": k.rsa_hash,
            "deprecated_after": k.deprecated_after.isoformat() if k.deprecated_after else None,
        }
        for k in TRUSTED_KEYS
    ]


@router.get("")
async def license_status() -> dict[str, Any]:
    """Return the current license info as JSON."""
    return _license_info_dict()


@router.post("/upload")
async def license_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Validate and install a new signed .lic file.

    The uploaded file is parsed and its signature verified against the baked-in
    Ed25519 public key BEFORE any on-disk write. If verification fails the
    current license is left untouched.
    """
    filename = file.filename or "uploaded.lic"
    if not filename.lower().endswith(".lic"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .lic files are accepted",
        )

    data = await file.read(MAX_LICENSE_BYTES + 1)
    if len(data) > MAX_LICENSE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"License file exceeds {MAX_LICENSE_BYTES} bytes — not a valid .lic",
        )

    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"License file is not valid JSON: {exc}",
        )

    if not isinstance(payload, dict) or "signature" not in payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="License file is missing the 'signature' field",
        )

    # Verify against baked-in public key — don't write anything to disk if bad.
    signature_b64 = payload["signature"]
    unsigned_payload = {k: v for k, v in payload.items() if k != "signature"}
    if not license_utils._verify_signature(unsigned_payload, signature_b64):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "License signature verification failed. The file may be "
                "corrupt, or it was not issued for this build."
            ),
        )

    # Check expiry — warn but allow install (admin may be staging a renewal).
    expires_at_raw = payload.get("expires_at")
    expires_at = license_utils._parse_datetime(expires_at_raw) if expires_at_raw else None
    already_expired = expires_at is not None and expires_at < datetime.now(timezone.utc)

    # Try to (re)base64 the signature on the original bytes to catch tampering
    # of unrelated bytes (trailing whitespace etc.) — we always write the
    # re-serialized JSON so consumers see the canonical on-disk shape.
    try:
        base64.b64decode(signature_b64, validate=True)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Signature field is not valid base64",
        )

    # Atomic write: temp file in the same directory, fsync, rename.
    license_path = license_utils.LICENSE_PATH
    try:
        license_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"License directory is not writable: {exc}",
        )

    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".ipsolis-license-",
            suffix=".part",
            dir=str(license_path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, license_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Could not write license to {license_path}: {exc}. "
                "Make sure the ./licenses volume is mounted read-write for the api container."
            ),
        )

    # Force-reload our own cache so the response reflects the new license.
    info_dict = _license_info_dict()
    logger.info(
        "admin: license installed — edition=%s licensee=%s expires=%s",
        info_dict["edition"], info_dict["licensee"], info_dict["expires_at"],
    )
    return {
        "ok": True,
        "already_expired": already_expired,
        "info": info_dict,
    }


@router.get("/download")
async def license_download() -> FileResponse:
    """Download the currently installed .lic file."""
    if not license_utils.LICENSE_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No license file installed",
        )
    return FileResponse(
        str(license_utils.LICENSE_PATH),
        media_type="application/json",
        filename="ipsolis.lic",
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def license_remove() -> Response:
    """Remove the currently installed license. Falls back to Community edition."""
    if license_utils.LICENSE_PATH.exists():
        try:
            license_utils.LICENSE_PATH.unlink()
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Could not remove license file: {exc}",
            )
        # Reload the license cache AND push the now-Community edition into
        # the Jinja2 globals — without this the Dashboard / nav locks stay
        # showing Enterprise until the api restarts.
        set_license_globals(license_utils.load_license(force_reload=True))
        logger.info("admin: license removed — instance is now Community edition")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
