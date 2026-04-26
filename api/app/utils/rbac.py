"""Role-based access control — RBAC slice 1.

Five-tier role ladder, ordered by privilege. Each role inherits the
visibility (but not necessarily the write capabilities) of every role
below it; ``require_role()`` checks against this ladder, so granting
``admin`` automatically grants any check that wants ``approver`` or
``auditor`` or ``helpdesk``.

Role intent (slice-1 enforcement summary in parentheses):

* ``superadmin`` — owns the platform, including admin user CRUD
  (full write access; only role allowed to manage other admin users).
* ``admin`` — operational owner; configures asset types, runbooks,
  scheduled tasks, integrations (full operational write; cannot
  manage admin users).
* ``approver`` — sign-off only; can decide approvals and read order
  state (slice-1 enforces only on routes that require it; broader
  approval-routing gating is slice-2).
* ``auditor`` — read-only; sees audit log + everything else, mutates
  nothing.
* ``helpdesk`` — narrow operational role — revoke / cancel orders,
  read pool state.

The legacy ``ADMIN_API_KEY`` (header) and the bearer-token path bypass
role checks: tokens use scopes instead, and the legacy key is treated
as a virtual ``superadmin`` for back-compat with existing scripts.
Role enforcement targets the **session** path (Admin UI users); slice-2
will extend role-binding to API tokens.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

# Ordered most-privileged → least-privileged.
ROLE_HIERARCHY = ("superadmin", "admin", "approver", "auditor", "helpdesk")
VALID_ROLES = frozenset(ROLE_HIERARCHY)


def role_at_least(actual: str | None, required: str) -> bool:
    """Return True if ``actual`` is at or above ``required`` in the ladder.

    Unknown actual roles return False (deny by default). An unknown
    required role raises ``ValueError`` since that's a programming
    error in the calling decorator, not user input.
    """
    if required not in VALID_ROLES:
        raise ValueError(f"Unknown role: {required!r}")
    if actual not in VALID_ROLES:
        return False
    return ROLE_HIERARCHY.index(actual) <= ROLE_HIERARCHY.index(required)


def require_role(required: str):
    """FastAPI dependency factory: gate by minimum role on the session.

    Use **alongside** ``require_admin_key`` (typically inherited from
    a router-level dependency). The chain is:

    1. ``require_admin_key`` populates ``request.state.actor`` and,
       on the bearer-token path, ``request.state.api_token``.
    2. ``require_role(required)`` reads ``request.session["admin_role"]``
       (set by the login flow) and compares it to ``required``.

    Bypass paths (back-compat / orthogonal authz):

    * ``admin:legacy_key`` — virtual superadmin; passes any role check.
    * Bearer tokens — scope-gated separately via ``require_scopes``;
      role check is a no-op.

    Otherwise: missing or insufficient role → HTTP 403.
    """
    if required not in VALID_ROLES:
        raise ValueError(f"Unknown role: {required!r}")

    async def _checked(request: Request) -> None:
        actor = getattr(request.state, "actor", "") or ""
        # Legacy key acts as an implicit superadmin so existing scripts
        # don't 403 the moment a route grows a role gate.
        if actor.startswith("admin:legacy_key"):
            return
        # Bearer tokens — RBAC slice 3: respect the token's bound role
        # if one was set at issue time. NULL token role bypasses the
        # check (pre-slice-3 behaviour: scopes alone govern such tokens).
        if actor.startswith("token:"):
            token = getattr(request.state, "api_token", None)
            token_role = getattr(token, "role", None)
            if token_role is None:
                return  # legacy / unbound token — scope checks rule
            if role_at_least(token_role, required):
                return
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Token role '{token_role}' is below the required '{required}'. "
                    f"Re-issue the token with a higher role or use a different token."
                ),
            )

        role = request.session.get("admin_role") or ""
        if role_at_least(role, required):
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Role '{role or '(none)'}' is below the required '{required}'. "
                f"Ask a superadmin to grant access."
            ),
        )

    return Depends(_checked)
