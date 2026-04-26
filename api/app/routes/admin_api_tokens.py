"""Admin endpoints for managing per-integration API tokens.

The raw token is returned **only** in the create response; afterward only
the prefix is visible in the list. Revocation is a soft delete (sets
``revoked_at``); we keep the row so historical audit attribution by name
still resolves.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.api_token import ApiToken
from app.utils.api_tokens import (
    AVAILABLE_SCOPES,
    create_token,
    filter_valid_scopes,
    status as token_status,
)
from app.utils.auth import require_admin_key
from app.utils.rbac import VALID_ROLES, role_at_least, require_role

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/admin/api-tokens",
    tags=["admin-api-tokens"],
    # Issuing or revoking integration tokens is an authority-level
    # change — same trust threshold as managing admin users.
    dependencies=[Depends(require_admin_key), require_role("superadmin")],
)


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    # Empty/missing → defaults to ``["admin:*"]`` for back-compat with the
    # slice-1 token UX. Unknown scopes are filtered out silently.
    scopes: list[str] | None = None
    # RBAC slice 3: optional role binding. NULL = scope-only authz
    # (back-compat for existing callers that don't pass this field).
    role: str | None = Field(default=None, max_length=32)


class TokenRow(BaseModel):
    id: int
    name: str
    token_prefix: str
    scopes: list[str]
    role: str | None
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    status: str

    model_config = {"from_attributes": True}


class TokenCreated(TokenRow):
    raw_token: str  # Plaintext token — only present on creation response


def _to_row(t: ApiToken) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "token_prefix": t.token_prefix,
        "scopes": list(t.scopes or []),
        "role": t.role,
        "created_by": t.created_by,
        "created_at": t.created_at,
        "expires_at": t.expires_at,
        "last_used_at": t.last_used_at,
        "revoked_at": t.revoked_at,
        "status": token_status(t),
    }


def _creator_role(request: Request) -> str:
    """Effective role of the creator for the role-mint guard.

    ``admin:legacy_key`` → virtual superadmin (matches the role-bypass
    semantics elsewhere). Bearer-token creators take their own role
    when set, else ``superadmin`` (since they hold ``admin:*`` scope
    today which is implicit superadmin pre-slice-3). Session users
    take their session role.
    """
    actor = getattr(request.state, "actor", "") or ""
    if actor.startswith("admin:legacy_key"):
        return "superadmin"
    if actor.startswith("token:"):
        token = getattr(request.state, "api_token", None)
        return getattr(token, "role", None) or "superadmin"
    return (request.session.get("admin_role") or "").strip() or "superadmin"


@router.get("", response_model=list[TokenRow])
async def list_tokens(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = await db.execute(select(ApiToken).order_by(ApiToken.created_at.desc()))
    return [_to_row(t) for t in rows.scalars().all()]


@router.get("/scopes")
async def list_scopes() -> dict:
    """Return the scope catalog so the UI can render checkboxes dynamically."""
    return {"scopes": [{"name": k, "description": v} for k, v in AVAILABLE_SCOPES.items()]}


@router.post("", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
async def create_api_token(
    payload: TokenCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    expires_at: datetime | None = None
    if payload.expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)

    actor = getattr(request.state, "actor", "admin:unknown")
    requested_scopes = filter_valid_scopes(payload.scopes) or ["admin:*"]

    # RBAC slice 3: optional role binding + mint guard. The creator can
    # only issue tokens at or below their own role — superadmin can mint
    # any role, an ``admin`` can't mint a superadmin-bound token.
    requested_role: str | None = None
    if payload.role:
        if payload.role not in VALID_ROLES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"role must be one of {sorted(VALID_ROLES)} or null",
            )
        creator_role = _creator_role(request)
        # Token role must be at-or-below creator role. ``role_at_least(creator, payload.role)``
        # returns True when creator is at least as privileged as the payload role.
        if not role_at_least(creator_role, payload.role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Cannot mint a token with role '{payload.role}' — your role "
                    f"'{creator_role}' is not privileged enough."
                ),
            )
        requested_role = payload.role

    token, raw = await create_token(
        db,
        name=payload.name,
        created_by=actor,
        expires_at=expires_at,
        scopes=requested_scopes,
        role=requested_role,
    )
    await db.commit()
    await db.refresh(token)
    logger.info(
        "admin: created API token id=%s name=%r role=%s by=%s",
        token.id, token.name, requested_role, actor,
    )

    out = _to_row(token)
    out["raw_token"] = raw  # one-time reveal
    return out


@router.delete(
    "/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def revoke_api_token(
    token_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    result = await db.execute(select(ApiToken).where(ApiToken.id == token_id))
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    if token.revoked_at is None:
        token.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        actor = getattr(request.state, "actor", "admin:unknown")
        logger.info("admin: revoked API token id=%s name=%r by=%s", token.id, token.name, actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
