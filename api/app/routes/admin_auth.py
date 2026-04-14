"""Admin UI authentication routes (login / logout).

These routes have NO auth dependency — they must be accessible without a session.
Registered under /ui prefix, included in main.py before ui.router.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.config import settings
from app.templates_instance import templates

router = APIRouter(prefix="/ui", tags=["admin-auth"])


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    """Renders the admin login form."""
    if request.session.get("admin_authenticated"):
        return RedirectResponse(url="/ui/", status_code=302)
    return templates.TemplateResponse("admin/login.html", {
        "request": request,
        "error": None,
    })


@router.post("/login", response_class=HTMLResponse)
async def admin_login_submit(request: Request, password: str = Form(...)):
    """Validates the admin password and establishes a session."""
    if password == settings.ADMIN_API_KEY:
        next_url = request.session.pop("admin_next", "/ui/")
        request.session["admin_authenticated"] = True
        return RedirectResponse(url=next_url, status_code=303)

    return templates.TemplateResponse("admin/login.html", {
        "request": request,
        "error": "Incorrect password.",
    }, status_code=401)


@router.post("/logout")
async def admin_logout(request: Request):
    """Clears the admin session and redirects to the login page."""
    request.session.clear()
    return RedirectResponse(url="/ui/login", status_code=303)
