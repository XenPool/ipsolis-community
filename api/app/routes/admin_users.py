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
from app.models.admin_user_grant import AdminUserAssetTypeGrant
from app.models.asset import AssetType
from app.utils.audit import aaudit, actor_by
from app.utils.auth import require_admin_key
from app.utils.password import hash_password, verify_password
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


# ── Per-asset-type ACL grants (RBAC slice 2) ──────────────────────────────────


class GrantRow(BaseModel):
    asset_type_id: int
    asset_type_name: str
    created_at: datetime
    created_by: str

    model_config = {"from_attributes": True}


class GrantSet(BaseModel):
    """Whole-set replacement for a user's asset-type grants.

    Sending an empty ``asset_type_ids`` deliberately clears all grants
    — the user flips back into "see all" (back-compat) mode. Senders
    that only want to *add* a grant should fetch the current set first,
    extend it, and PUT the merged list.
    """
    asset_type_ids: list[int]


@router.get("/{user_id}/grants", response_model=list[GrantRow])
async def list_user_grants(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List every asset-type grant the user holds (joined with type names)."""
    user_res = await db.execute(select(AdminUser).where(AdminUser.id == user_id))
    if user_res.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    rows = await db.execute(
        select(
            AdminUserAssetTypeGrant.asset_type_id,
            AssetType.name,
            AdminUserAssetTypeGrant.created_at,
            AdminUserAssetTypeGrant.created_by,
        )
        .join(AssetType, AssetType.id == AdminUserAssetTypeGrant.asset_type_id)
        .where(AdminUserAssetTypeGrant.admin_user_id == user_id)
        .order_by(AssetType.name)
    )
    return [
        {
            "asset_type_id": r[0],
            "asset_type_name": r[1],
            "created_at": r[2],
            "created_by": r[3],
        }
        for r in rows.all()
    ]


@router.put("/{user_id}/grants", response_model=list[GrantRow])
async def set_user_grants(
    request: Request,
    user_id: int,
    payload: GrantSet,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Replace a user's grant set wholesale.

    Idempotent: sending the same set twice is a no-op (other than
    audit). Validates every supplied id against ``asset_types`` so a
    typo can't silently create a dangling grant.
    """
    user_res = await db.execute(select(AdminUser).where(AdminUser.id == user_id))
    user = user_res.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    requested = {int(i) for i in payload.asset_type_ids if isinstance(i, int) and i > 0}
    if requested:
        existing_types = await db.execute(
            select(AssetType.id).where(AssetType.id.in_(requested))
        )
        valid_ids = {r for r in existing_types.scalars().all()}
        missing = sorted(requested - valid_ids)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown asset_type id(s): {missing}",
            )

    # Diff against current state so the audit row is informative.
    current_rows = await db.execute(
        select(AdminUserAssetTypeGrant.asset_type_id).where(
            AdminUserAssetTypeGrant.admin_user_id == user_id
        )
    )
    current = {int(r) for r in current_rows.scalars().all()}
    to_add = requested - current
    to_remove = current - requested

    if to_remove:
        from sqlalchemy import delete as sa_delete
        await db.execute(
            sa_delete(AdminUserAssetTypeGrant).where(
                AdminUserAssetTypeGrant.admin_user_id == user_id,
                AdminUserAssetTypeGrant.asset_type_id.in_(to_remove),
            )
        )
    actor = actor_by(request, "set_admin_user_grants")
    for tid in sorted(to_add):
        db.add(AdminUserAssetTypeGrant(
            admin_user_id=user_id,
            asset_type_id=tid,
            created_by=actor,
        ))

    if to_add or to_remove:
        await aaudit(
            db, "admin_user", user.id, "grants_updated",
            old={"asset_type_ids": sorted(current)},
            new={"asset_type_ids": sorted(requested)},
            by=actor,
        )
    await db.commit()
    logger.info(
        "Admin user %s grants updated: +%s -%s",
        user.username, sorted(to_add), sorted(to_remove),
    )
    return await list_user_grants(user.id, db)
