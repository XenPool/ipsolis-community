from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db

_api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)
_authorization_header = APIKeyHeader(name="Authorization", auto_error=False)


async def require_admin_key(
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str | None = Security(_api_key_header),
    authorization: str | None = Security(_authorization_header),
) -> None:
    """Dependency: validates either of three credential paths:

    1. ``X-Admin-Key: <ADMIN_API_KEY>`` (legacy env-driven shared key)
    2. Admin session cookie (browser UI flow)
    3. ``Authorization: Bearer <xpat_…>`` (per-integration token from
       the ``api_tokens`` table)

    On success stores attribution metadata on ``request.state`` so
    audit handlers can see "which token did this":

    * ``request.state.actor``      = "admin:legacy_key" / "admin:session" / "token:<name>"
    * ``request.state.api_token``  = ``ApiToken`` ORM row when path 3 was used
    """
    # Path 1: legacy env shared key
    if api_key and api_key == settings.ADMIN_API_KEY:
        request.state.actor = "admin:legacy_key"
        return

    # Path 2: admin session
    if request.session.get("admin_authenticated"):
        admin_user = request.session.get("admin_user") or "admin"
        admin_role = request.session.get("admin_role") or ""
        # Attribution carries the role so the audit log lets auditors
        # filter by both *who* made the change and *with what authority*
        # — `admin:session:alice:admin` reads top-down by privilege.
        if admin_role:
            request.state.actor = f"admin:session:{admin_user}:{admin_role}"
        else:
            request.state.actor = f"admin:session:{admin_user}"
        return

    # Path 3: bearer token from api_tokens
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
        from app.utils.api_tokens import mark_used, verify_raw_token

        token = await verify_raw_token(db, raw)
        if token is not None:
            await mark_used(db, token.id)
            await db.commit()
            request.state.api_token = token
            request.state.actor = f"token:{token.name}"
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required (X-Admin-Key, session, or Bearer token).",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def attribute_actor_if_present(
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key: str | None = Security(_api_key_header),
    authorization: str | None = Security(_authorization_header),
) -> None:
    """Soft-auth dependency: populate ``request.state.actor`` *if* valid
    credentials are provided, without ever raising on missing or invalid ones.

    Use on intentionally-public routes (e.g. ``/orders/``, ``/webhook/``)
    so that when a caller *does* present a valid token / admin key, the
    audit log records who they are — but anonymous callers continue to
    work unchanged. The route handler should keep using ``actor_by()`` as
    usual; that helper falls back to ``api:<label>`` when no actor is
    populated.

    Mirrors the recognition logic from ``require_admin_key`` exactly so
    the two paths stay in sync. Bad credentials (bogus token, wrong
    admin key) are silently ignored at this level — the route handler
    owns the decision of whether to enforce auth.
    """
    if api_key and api_key == settings.ADMIN_API_KEY:
        request.state.actor = "admin:legacy_key"
        return
    if request.session.get("admin_authenticated"):
        admin_user = request.session.get("admin_user") or "admin"
        request.state.actor = f"admin:session:{admin_user}"
        return
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
        from app.utils.api_tokens import mark_used, verify_raw_token  # noqa: PLC0415

        token = await verify_raw_token(db, raw)
        if token is not None:
            await mark_used(db, token.id)
            await db.commit()
            request.state.api_token = token
            request.state.actor = f"token:{token.name}"
            return
    # No usable creds — leave request.state.actor unset; ``actor_by``
    # will fall back to the bare ``api:<label>`` form.


async def require_admin_session(request: Request) -> None:
    """Dependency: validates admin session cookie for browser-based UI access.

    Redirects unauthenticated requests to /ui/login, preserving the intended URL.
    """
    if not request.session.get("admin_authenticated"):
        request.session["admin_next"] = str(request.url)
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/ui/login"},
        )


def require_scopes(*needed: str):
    """FastAPI dependency factory: enforce token scopes on top of auth.

    Use **alongside** ``require_admin_key`` (typically inherited from a
    router-level dependency). This dependency assumes auth has already
    populated ``request.state.actor`` and, on the bearer-token path,
    ``request.state.api_token``. For routes that aren't already
    auth-protected, also list ``Depends(require_admin_key)`` first.

    Legacy ``X-Admin-Key`` and admin sessions are intentionally
    unconstrained (back-compat with existing integrations and the UI).
    Bearer tokens with ``admin:*`` grant everything; otherwise every
    scope in ``needed`` must be present.
    """
    async def _scoped(request: Request) -> None:
        from app.utils.api_tokens import token_has_scope

        actor = getattr(request.state, "actor", "") or ""
        if actor.startswith("admin:legacy_key") or actor.startswith("admin:session"):
            return  # legacy path — implicit admin:*

        token = getattr(request.state, "api_token", None)
        if token is None:
            # Defensive: should be unreachable when require_admin_key has
            # already run. Reaching here means a route wired this scope
            # check without an auth dependency in its chain.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        scopes = list(token.scopes or [])
        missing = [s for s in needed if not token_has_scope(scopes, s)]
        if missing:
            granted = ", ".join(sorted(scopes)) or "(none)"
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Token '{token.name}' is missing required scope(s): "
                    f"{', '.join(missing)}. Granted: {granted}."
                ),
            )

    return Depends(_scoped)
