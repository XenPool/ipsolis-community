from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import settings

_api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def require_admin_key(api_key: str | None = Security(_api_key_header)) -> None:
    """Dependency: validates X-Admin-Key header. Disabled in dev mode."""
    if settings.is_development or settings.ADMIN_AUTH_DISABLED:
        return
    if not api_key or api_key != settings.ADMIN_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Admin-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
