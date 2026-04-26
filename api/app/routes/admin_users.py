"""Admin endpoints for managing admin user accounts (RBAC slice 1).

Restricted to ``superadmin`` — only superadmins can mint, demote, or
deactivate other admin users. Passwords are hashed with PBKDF2-SHA256
on write; the plaintext is never stored or logged.

Slice scope:
- Create / list / change-role / change-password / activate-toggle /
  delete (soft, by setting ``is_active=False``).
- Self-protection: a superadmin can't delete or demote themselves —
  prevents accidental lockout. Removing the *last* superadmin is also
  blocked (any of those operations).

Out of scope (slice 2):
- Per-asset-type ACL grants on each user.
- Self-service password change for non-superadmins.
- Forced password rotation policies.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.admin_user import AdminUser
from app.utils.audit import aaudit, actor_by
from app.utils.auth import require_admin_key
from app.utils.password import hash_password
from app.utils.rbac import VALID_ROLES, require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/admin-users",
    tags=["admin-admin-users"],
    dependencies=[Depends(require_admin_key), require_role("superadmin")],
)


_USERNAME_ALLOWED_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._@-")


def _normalise_username(raw: str) -> str:
    norm = (raw or "").strip().lower()
    if not (3 <= len(norm) <= 128):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Username must be 3-128 characters.",
        )
    if not all(c in _USERNAME_ALLOWED_CHARS for c in norm):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Username may contain letters, digits, dot, underscore, '@', and hyphen.",
        )
    return norm


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=12, max_length=256)
    role: str = Field(default="admin")

    @field_validator("role")
    @classmethod
    def _role_in_set(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(
                f"role must be one of {sorted(VALID_ROLES)}",
            )
        return v


class AdminUserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    # Optional password rotation. Empty / missing → no change.
    new_password: str | None = Field(default=None, min_length=12, max_length=256)

    @field_validator("role")
    @classmethod
    def _role_in_set(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_ROLES:
            raise ValueError(
                f"role must be one of {sorted(VALID_ROLES)}",
            )
        return v


class AdminUserRow(BaseModel):
    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    created_by: str

    model_config = {"from_attributes": True}


async def _count_active_superadmins(db: AsyncSession) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(AdminUser)
        .where(AdminUser.role == "superadmin", AdminUser.is_active.is_(True))
    )
    return int(res.scalar_one())


def _self_username(request: Request) -> str | None:
    return (request.session.get("admin_user") or "").strip().lower() or None


@router.get("", response_model=list[AdminUserRow])
async def list_admin_users(db: AsyncSession = Depends(get_db)) -> list[AdminUser]:
    rows = await db.execute(select(AdminUser).order_by(AdminUser.username))
    return list(rows.scalars().all())


@router.post(
    "",
    response_model=AdminUserRow,
    status_code=status.HTTP_201_CREATED,
)
async def create_admin_user(
    request: Request,
    payload: AdminUserCreate,
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
    username = _normalise_username(payload.username)
    existing = await db.execute(
        select(AdminUser).where(AdminUser.username == username)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Admin user {username!r} already exists.",
        )
    user = AdminUser(
        username=username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=True,
        created_by=actor_by(request, "create_admin_user"),
    )
    db.add(user)
    await db.flush()
    await aaudit(
        db, "admin_user", user.id, "created",
        new={"username": user.username, "role": user.role, "is_active": True},
        by=actor_by(request, "create_admin_user"),
    )
    await db.commit()
    await db.refresh(user)
    logger.info("Admin user created: id=%s username=%s role=%s", user.id, user.username, user.role)
    return user


@router.put("/{user_id}", response_model=AdminUserRow)
async def update_admin_user(
    request: Request,
    user_id: int,
    payload: AdminUserUpdate,
    db: AsyncSession = Depends(get_db),
) -> AdminUser:
    res = await db.execute(select(AdminUser).where(AdminUser.id == user_id))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    me = _self_username(request)
    is_self = (me == user.username)

    old_snap = {
        "username": user.username, "role": user.role, "is_active": user.is_active,
    }
    changes: dict = {}

    # Role change — guard against self-demotion and last-superadmin loss.
    if payload.role is not None and payload.role != user.role:
        if is_self and payload.role != "superadmin":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A superadmin cannot demote themselves; use another superadmin account.",
            )
        if user.role == "superadmin" and payload.role != "superadmin":
            if (await _count_active_superadmins(db)) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot demote the last active superadmin.",
                )
        changes["role"] = payload.role
        user.role = payload.role

    # Activation toggle — guard against deactivating the last active superadmin.
    if payload.is_active is not None and payload.is_active != user.is_active:
        if is_self and not payload.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A superadmin cannot deactivate themselves.",
            )
        if (
            user.role == "superadmin"
            and user.is_active
            and not payload.is_active
            and (await _count_active_superadmins(db)) <= 1
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot deactivate the last active superadmin.",
            )
        changes["is_active"] = payload.is_active
        user.is_active = payload.is_active

    if payload.new_password:
        user.password_hash = hash_password(payload.new_password)
        # Don't audit password contents; just record that a rotation happened.
        changes["password_changed"] = True

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No changes specified.",
        )

    new_snap = {
        "username": user.username, "role": user.role, "is_active": user.is_active,
        **({"password_changed": True} if changes.get("password_changed") else {}),
    }
    await aaudit(
        db, "admin_user", user.id, "updated",
        old=old_snap, new=new_snap,
        by=actor_by(request, "update_admin_user"),
    )
    await db.commit()
    await db.refresh(user)
    logger.info("Admin user updated: id=%s changes=%s", user.id, list(changes.keys()))
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_admin_user(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Hard-delete an admin user.

    Use ``PUT`` with ``is_active=false`` for soft revocation that keeps
    the audit-trail attribution resolvable. Hard delete is here for
    operators who really want the row gone (e.g. test users).
    """
    res = await db.execute(select(AdminUser).where(AdminUser.id == user_id))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    me = _self_username(request)
    if me == user.username:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A superadmin cannot delete themselves.",
        )
    if (
        user.role == "superadmin"
        and user.is_active
        and (await _count_active_superadmins(db)) <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete the last active superadmin.",
        )

    snap = {
        "username": user.username, "role": user.role, "is_active": user.is_active,
    }
    await aaudit(
        db, "admin_user", user.id, "deleted",
        old=snap,
        by=actor_by(request, "delete_admin_user"),
    )
    await db.delete(user)
    await db.commit()
    logger.info("Admin user deleted: id=%s username=%s", user_id, user.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
