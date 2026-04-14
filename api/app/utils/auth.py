from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.config import settings

_api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def require_admin_key(
    request: Request,
    api_key: str | None = Security(_api_key_header),
) -> None:
    """Dependency: validates X-Admin-Key header or admin session cookie."""
    if api_key and api_key == settings.ADMIN_API_KEY:
        return
    if request.session.get("admin_authenticated"):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing X-Admin-Key header",
        headers={"WWW-Authenticate": "ApiKey"},
    )


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
