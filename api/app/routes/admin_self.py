"""Self-service account routes — RBAC slice 3.

Endpoints any logged-in admin user can use to manage their own
account, regardless of role: change password, view current session
identity. Distinct from ``/admin/admin-users/*`` (superadmin only —
manage *other* people's accounts).

The legacy ``ADMIN_API_KEY`` actor has no ``admin_users`` row and so
can't change "its" password through this endpoint — the underlying
secret lives in ``.env`` and rotation is an infrastructure operation.
The password-change endpoint detects that case and returns a
descriptive 409 instead of silently failing.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.admin_user import AdminUser
from app.utils.audit import aaudit, actor_by
from app.utils.auth import require_admin_key
from app.utils.password import hash_password, verify_password

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/admin/me",
    tags=["admin-self"],
    dependencies=[Depends(require_admin_key)],
)


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
    new_password_confirm: str = Field(min_length=12, max_length=256)


class WhoAmI(BaseModel):
    """Snapshot of the current admin session — useful for UI nav state."""
    username: str | None
    role: str | None
    via: str | None  # "user" | "legacy_key" | "token" | None
    is_anonymous_legacy_key: bool


@router.get("", response_model=WhoAmI)
async def whoami(request: Request) -> WhoAmI:
    """Return identity metadata for the current actor.

    Useful for the UI to render the current user's name / role and to
    decide which links to show. Doesn't 404 on legacy-key callers —
    they get a row with ``via=legacy_key`` and ``is_anonymous_legacy_key=True``
    so the UI can hint that "change password" isn't available.
    """
    actor = (getattr(request.state, "actor", "") or "")
    if actor.startswith("admin:legacy_key"):
        return WhoAmI(
            username=None, role="superadmin",
            via="legacy_key", is_anonymous_legacy_key=True,
        )
    if actor.startswith("token:"):
        token = getattr(request.state, "api_token", None)
        return WhoAmI(
            username=getattr(token, "name", None),
            role=getattr(token, "role", None),
            via="token", is_anonymous_legacy_key=False,
        )
    return WhoAmI(
        username=request.session.get("admin_user"),
        role=request.session.get("admin_role"),
        via="user", is_anonymous_legacy_key=False,
    )


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def change_my_password(
    request: Request,
    payload: ChangePasswordPayload,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Change the password of the currently-logged-in admin user.

    Requires the current password as a liveness check (defends against
    session-hijack-followed-by-password-pivot, since the attacker
    needs the old password to lock the legitimate user out).
    """
    if payload.new_password != payload.new_password_confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="New passwords do not match.",
        )

    actor = (getattr(request.state, "actor", "") or "")
    if actor.startswith("admin:legacy_key"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The legacy ADMIN_API_KEY isn't backed by an admin_users row "
                "— rotate it via your .env / secret manager instead."
            ),
        )
    if actor.startswith("token:"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "API tokens don't have passwords. Revoke and re-issue the "
                "token to rotate."
            ),
        )

    username = (request.session.get("admin_user") or "").strip().lower()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No admin user on session.",
        )

    res = await db.execute(
        select(AdminUser).where(AdminUser.username == username)
    )
    user = res.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account no longer exists or is deactivated. Sign in again.",
        )

    if not verify_password(payload.current_password, user.password_hash):
        # Don't disambiguate "wrong current password" from anything
        # else with extra info — just deny.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="New password must differ from the current one.",
        )

    user.password_hash = hash_password(payload.new_password)
    # Audit trail without leaking either password — record only that a
    # rotation happened, not the values.
    await aaudit(
        db, "admin_user", user.id, "password_changed_self",
        new={"by_self": True},
        by=actor_by(request, "change_my_password"),
    )
    await db.commit()
    logger.info("Self-service password change for admin user %s", user.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
