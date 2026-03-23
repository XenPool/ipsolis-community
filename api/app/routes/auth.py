"""Portal authentication routes — Entra ID OAuth2/OIDC auth code flow.

Routes:
  GET /portal/login           → redirect to Entra ID
  GET /portal/auth/callback   → exchange code, set session, redirect to portal
  GET /portal/logout          → clear session, redirect to Entra ID logout
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.utils import entra as entra_utils

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portal", tags=["auth"])
templates = Jinja2Templates(directory="/app/app/templates")


@router.get("/login", response_class=RedirectResponse)
async def portal_login(request: Request, db: AsyncSession = Depends(get_db)):
    """Redirects the user to the Entra ID authorization endpoint."""
    cfg = await entra_utils._get_entra_config(db)
    msal_app = entra_utils.get_msal_app(cfg)

    if msal_app is None:
        # Entra not configured — send back to portal (bypass)
        return RedirectResponse(url="/portal/", status_code=302)

    redirect_uri = cfg.get("entra.redirect_uri", "").strip() or str(
        request.url_for("portal_auth_callback")
    )
    state = entra_utils.new_state()
    request.session["oauth_state"] = state

    auth_url = entra_utils.build_auth_url(msal_app, redirect_uri=redirect_uri, state=state)
    logger.info("[auth] Redirecting to Entra ID login")
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/auth/callback", response_class=HTMLResponse, name="portal_auth_callback")
async def portal_auth_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """Handles the Entra ID redirect after successful (or failed) authentication."""
    # Show error page on Entra-side failures
    if error:
        logger.warning("[auth] Entra ID returned error: %s – %s", error, error_description)
        return templates.TemplateResponse(
            request, "portal/auth_error.html",
            {"title": "Login failed", "message": error_description or error},
            status_code=401,
        )

    # CSRF state check
    expected_state = request.session.pop("oauth_state", None)
    if not state or state != expected_state:
        logger.warning("[auth] OAuth state mismatch — possible CSRF attempt")
        return templates.TemplateResponse(
            request, "portal/auth_error.html",
            {"title": "Login failed", "message": "Invalid state parameter. Please try again."},
            status_code=400,
        )

    cfg = await entra_utils._get_entra_config(db)
    msal_app = entra_utils.get_msal_app(cfg)
    if msal_app is None:
        return RedirectResponse(url="/portal/", status_code=302)

    redirect_uri = cfg.get("entra.redirect_uri", "").strip() or str(
        request.url_for("portal_auth_callback")
    )

    try:
        token_response = entra_utils.exchange_code(msal_app, code=code, redirect_uri=redirect_uri)
    except ValueError as exc:
        logger.error("[auth] Token exchange failed: %s", exc)
        return templates.TemplateResponse(
            request, "portal/auth_error.html",
            {"title": "Login failed", "message": str(exc)},
            status_code=401,
        )

    user = entra_utils.extract_portal_user(token_response)

    # Domain restriction check
    allowed_domains = cfg.get("entra.allowed_domains", "")
    if not entra_utils.check_allowed_domains(user, allowed_domains):
        logger.warning("[auth] Login rejected — domain not allowed: %s", user.get("upn"))
        return templates.TemplateResponse(
            request, "portal/auth_error.html",
            {
                "title": "Access denied",
                "message": (
                    f"Your account ({user.get('upn')}) is not permitted to access this portal. "
                    "Please contact your IT administrator."
                ),
            },
            status_code=403,
        )

    request.session["portal_user"] = user
    logger.info("[auth] Login successful: %s (%s)", user.get("name"), user.get("email"))

    next_url = request.session.pop("login_next", "/portal/")
    return RedirectResponse(url=next_url, status_code=302)


@router.get("/logout")
async def portal_logout(request: Request, db: AsyncSession = Depends(get_db)):
    """Clears the session and redirects to the Entra ID logout endpoint."""
    cfg = await entra_utils._get_entra_config(db)
    tenant_id = cfg.get("entra.tenant_id", "").strip()

    request.session.clear()

    if tenant_id:
        entra_logout_url = (
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/logout"
            "?post_logout_redirect_uri=/portal/login"
        )
        return RedirectResponse(url=entra_logout_url, status_code=302)

    return RedirectResponse(url="/portal/login", status_code=302)
