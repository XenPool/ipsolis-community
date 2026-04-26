"""Issue, hash, and verify API tokens.

The raw token is shown to the user **once** at creation time. We store
only its SHA-256 in the database, so a database leak doesn't expose
working credentials. Tokens have an unmistakable ``xpat_`` prefix
(ipsolis admin token) so they're easy to spot in logs and config files.

Scope catalog (``AVAILABLE_SCOPES``) defines the per-resource permissions
a token can carry. ``admin:*`` is the wildcard granted by legacy
``X-Admin-Key`` and admin sessions for back-compat — narrowly-scoped
tokens (``orders:read`` etc.) are the recommended path for new
integrations.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_token import ApiToken

logger = logging.getLogger(__name__)

_PREFIX = "xpat_"
_RAW_BYTES = 32  # 256 bits → 43-char URL-safe base64 → token length 48 with prefix


# Scope catalog. Keys are the scope strings stored on the token; values are
# human-readable labels shown in the create-token modal. Group prefix
# (``orders:``, ``asset_types:`` …) makes it easy to skim and to add new
# scopes alongside the resource they protect.
AVAILABLE_SCOPES: dict[str, str] = {
    "admin:*":           "Full admin access — equivalent to legacy X-Admin-Key.",
    "orders:read":       "List and view orders.",
    "orders:write":      "Create, update, cancel orders.",
    "asset_types:read":  "List and view asset definitions.",
    "asset_types:write": "Create, edit, delete asset definitions.",
    "assets:read":       "View the asset pool.",
    "assets:write":      "Manage assets in the pool.",
    "approvals:read":    "View pending approvals.",
    "approvals:write":   "Approve or decline requests.",
    "audit:read":        "Read the audit log.",
    "config:read":       "Read application settings.",
    "config:write":      "Modify application settings.",
    "metrics:read":      "Scrape the /metrics Prometheus endpoint.",
    "webhook:in":        "Inbound webhook receiver (ServiceNow et al.).",
}


def is_valid_scope(scope: str) -> bool:
    return scope in AVAILABLE_SCOPES


def filter_valid_scopes(scopes: list[str] | None) -> list[str]:
    """Drop unknown scopes; preserve order; deduplicate."""
    seen: set[str] = set()
    out: list[str] = []
    for s in scopes or []:
        if s in AVAILABLE_SCOPES and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def token_has_scope(token_scopes: list[str] | None, needed: str) -> bool:
    """``True`` if the token grants ``needed``, including via the ``admin:*`` wildcard."""
    if not token_scopes:
        return False
    if "admin:*" in token_scopes:
        return True
    return needed in token_scopes


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_raw_token() -> tuple[str, str]:
    """Returns ``(raw_token, token_prefix)``.

    ``raw_token`` is what the user copies (and never sees again).
    ``token_prefix`` is the first six chars used in the UI for
    "which token did this" without revealing the secret.
    """
    raw = _PREFIX + secrets.token_urlsafe(_RAW_BYTES)
    return raw, raw[:6]


async def create_token(
    db: AsyncSession,
    *,
    name: str,
    created_by: str,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    role: str | None = None,
) -> tuple[ApiToken, str]:
    """Issue a fresh token. Returns ``(orm_row, raw_token)``.

    The raw token is only available in this function's return value — it
    is never persisted in plaintext. Caller is responsible for showing
    it to the user once and committing the session.

    ``role`` (RBAC slice 3) is optional. When set, the token is gated
    by ``require_role`` against this role in addition to its scopes.
    NULL keeps the pre-slice-3 scope-only behaviour.
    """
    raw, prefix = generate_raw_token()
    row = ApiToken(
        name=name.strip(),
        token_hash=_hash(raw),
        token_prefix=prefix,
        scopes=scopes or ["admin:*"],
        created_by=created_by,
        expires_at=expires_at,
        role=role,
    )
    db.add(row)
    await db.flush()
    return row, raw


async def verify_raw_token(db: AsyncSession, raw: str) -> ApiToken | None:
    """Look up a raw token. Returns the ORM row when valid, else ``None``.

    "Valid" means: row exists, not revoked, and not past ``expires_at``.
    Last-used timestamp updates are best-effort (in a separate function
    so the caller can fire-and-forget).
    """
    if not raw or not raw.startswith(_PREFIX):
        return None
    result = await db.execute(
        select(ApiToken).where(ApiToken.token_hash == _hash(raw))
    )
    token = result.scalar_one_or_none()
    if token is None:
        return None
    now = datetime.now(timezone.utc)
    if token.revoked_at is not None:
        return None
    if token.expires_at is not None and token.expires_at < now:
        return None
    return token


async def mark_used(db: AsyncSession, token_id: int) -> None:
    """Best-effort last-used update. Caller commits."""
    try:
        await db.execute(
            update(ApiToken)
            .where(ApiToken.id == token_id)
            .values(last_used_at=datetime.now(timezone.utc))
        )
    except Exception as exc:  # noqa: BLE001 — never fail an authed request on a write hiccup
        logger.warning("api_tokens: mark_used failed for id=%s: %s", token_id, exc)


def status(token: ApiToken) -> str:
    """Human-readable status for UI display: active / expired / revoked."""
    if token.revoked_at is not None:
        return "revoked"
    if token.expires_at is not None and token.expires_at < datetime.now(timezone.utc):
        return "expired"
    return "active"
