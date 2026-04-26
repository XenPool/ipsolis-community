"""Admin UI authentication routes (login / logout / first-run setup).

These routes have NO auth dependency — they must be accessible without a session.
Registered under /ui prefix, included in main.py before ui.router.

RBAC slice 1: replaces the binary "single ADMIN_API_KEY = god mode"
login with per-user accounts. The legacy key still works as a
back-compat fallback (treated as superadmin); real users live in
``admin_users`` and authenticate with PBKDF2-hashed passwords.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.admin_user import AdminUser
from app.templates_instance import templates
from app.utils.password import hash_password, verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["admin-auth"])


async def _admin_users_count(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(AdminUser))
    return int(result.scalar_one())


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Renders the admin login form, or the first-run setup form when empty."""
    if request.session.get("admin_authenticated"):
        return RedirectResponse(url="/ui/", status_code=302)
    first_run = (await _admin_users_count(db)) == 0
    return templates.TemplateResponse("admin/login.html", {
        "request": request,
        "error": None,
        "first_run": first_run,
    })


@router.post("/login", response_class=HTMLResponse)
async def admin_login_submit(
    request: Request,
    password: str = Form(...),
    username: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Validates credentials and establishes a session.

    Two recognised paths:

    * Legacy: empty username + password matches ``settings.ADMIN_API_KEY``.
      Establishes a virtual ``superadmin`` session attributed as
      ``admin:legacy_key`` in the audit log.
    * Per-user: username + password matched against ``admin_users``
      with ``is_active = true``. Updates ``last_login_at`` on success.
      Session attribution is ``admin:session:<username>:<role>``.
    """
    username_norm = (username or "").strip().lower()

    # Legacy path — preserved verbatim so existing setups don't break.
    if not username_norm:
        if password == settings.ADMIN_API_KEY:
            next_url = request.session.pop("admin_next", "/ui/")
            request.session["admin_authenticated"] = True
            request.session["admin_user"] = "admin"
            request.session["admin_role"] = "superadmin"
            request.session["admin_via"] = "legacy_key"
            logger.info("Admin login: legacy key (back-compat)")
            return RedirectResponse(url=next_url, status_code=303)

    # Per-user path
    if username_norm:
        result = await db.execute(
            select(AdminUser).where(AdminUser.username == username_norm)
        )
        user = result.scalar_one_or_none()
        if user and user.is_active and verify_password(password, user.password_hash):
            user.last_login_at = datetime.now(timezone.utc)
            await db.commit()
            next_url = request.session.pop("admin_next", "/ui/")
            request.session["admin_authenticated"] = True
            request.session["admin_user"] = user.username
            request.session["admin_role"] = user.role
            request.session["admin_via"] = "user"
            logger.info("Admin login: user=%s role=%s", user.username, user.role)
            return RedirectResponse(url=next_url, status_code=303)

    # Fallback — re-render with error and first-run flag refreshed.
    first_run = (await _admin_users_count(db)) == 0
    return templates.TemplateResponse("admin/login.html", {
        "request": request,
        "error": "Incorrect username or password.",
        "first_run": first_run,
    }, status_code=401)


@router.post("/setup", response_class=HTMLResponse)
async def admin_first_run_setup(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Creates the first superadmin when ``admin_users`` is empty.

    Idempotent against races: re-checks the count inside the request and
    fails the form if a user has been created in the meantime (e.g. two
    operators hitting the setup form simultaneously).
    """
    if (await _admin_users_count(db)) > 0:
        return templates.TemplateResponse("admin/login.html", {
            "request": request,
            "error": "Setup already complete. Sign in instead.",
            "first_run": False,
        }, status_code=409)

    username_norm = (username or "").strip().lower()
    if (
        not username_norm
        or len(username_norm) < 3
        or len(username_norm) > 128
        or not all(c.isalnum() or c in "._@-" for c in username_norm)
    ):
        return templates.TemplateResponse("admin/login.html", {
            "request": request,
            "error": "Username must be 3-128 chars: letters, digits, dot, underscore, @, hyphen.",
            "first_run": True,
        }, status_code=422)
    if len(password or "") < 12:
        return templates.TemplateResponse("admin/login.html", {
            "request": request,
            "error": "Password must be at least 12 characters.",
            "first_run": True,
        }, status_code=422)
    if password != password_confirm:
        return templates.TemplateResponse("admin/login.html", {
            "request": request,
            "error": "Passwords do not match.",
            "first_run": True,
        }, status_code=422)

    user = AdminUser(
        username=username_norm,
        password_hash=hash_password(password),
        role="superadmin",
        is_active=True,
        created_by="first-run-setup",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    # Auto-login after setup so the operator lands on the dashboard.
    request.session["admin_authenticated"] = True
    request.session["admin_user"] = user.username
    request.session["admin_role"] = user.role
    request.session["admin_via"] = "user"
    logger.info("First-run setup: created superadmin %s", user.username)
    return RedirectResponse(url="/ui/", status_code=303)


@router.post("/logout")
async def admin_logout(request: Request):
    """Clears the admin session and redirects to the login page."""
    request.session.clear()
    return RedirectResponse(url="/ui/login", status_code=303)
