"""Entra ID / Azure AD helpers for portal SSO (MSAL auth code flow).

Config is read from app_config (entra.* keys), not from .env, so credentials
can be updated via the admin settings page without a restart.

entra.mode values:
  disabled            – no auth required (dev / before Entra setup)
  entra_only          – SSO required, no additional on-prem check
  entra_with_onprem   – SSO required + UPN validated against on-prem LDAP
"""

import logging
import secrets
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)

# Entra ID OIDC/OAuth2 endpoints
_AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}"
_SCOPES = ["openid", "profile", "email"]


async def _get_entra_config(db: AsyncSession) -> dict:
    """Loads all entra.* keys from app_config as a plain dict."""
    from app.models.config import AppConfig
    result = await db.execute(
        select(AppConfig).where(AppConfig.key.like("entra.%"))
    )
    return {row.key: (row.value or "") for row in result.scalars().all()}


def get_msal_app(cfg: dict):
    """Builds an MSAL ConfidentialClientApplication from config dict.

    Returns None if mode is 'disabled' or required keys are missing.
    """
    import msal

    mode = cfg.get("entra.mode", "disabled")
    if mode == "disabled":
        return None

    tenant_id = cfg.get("entra.tenant_id", "").strip()
    client_id = cfg.get("entra.client_id", "").strip()
    client_secret = cfg.get("entra.client_secret", "").strip()

    if not (tenant_id and client_id and client_secret):
        logger.warning(
            "[entra] Mode=%s but entra.tenant_id / client_id / client_secret not fully configured",
            mode,
        )
        return None

    authority = _AUTHORITY_TEMPLATE.format(tenant_id=tenant_id)
    return msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=authority,
    )


def build_auth_url(msal_app, redirect_uri: str, state: str) -> str:
    """Returns the Entra ID authorization URL to redirect the user to."""
    return msal_app.get_authorization_request_url(
        scopes=_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
    )


def exchange_code(msal_app, code: str, redirect_uri: str) -> dict:
    """Exchanges an auth code for tokens.

    Returns the token response dict on success.
    Raises ValueError with a human-readable message on failure.
    """
    result = msal_app.acquire_token_by_authorization_code(
        code=code,
        scopes=_SCOPES,
        redirect_uri=redirect_uri,
    )
    if "error" in result:
        raise ValueError(
            f"Token exchange failed: {result.get('error')} – {result.get('error_description', '')}"
        )
    return result


def extract_portal_user(token_response: dict) -> dict:
    """Extracts the portal user dict from an MSAL token response.

    Returns: {"email", "name", "oid", "upn"}
    """
    claims = token_response.get("id_token_claims", {})
    # preferred_username is the UPN in Entra ID (works for cloud + synced accounts)
    upn = claims.get("preferred_username") or claims.get("upn") or ""
    email = claims.get("email") or upn
    name = claims.get("name") or email.split("@")[0]
    oid = claims.get("oid") or claims.get("sub") or ""
    return {"email": email, "name": name, "oid": oid, "upn": upn}


def check_allowed_domains(user: dict, allowed_domains: str) -> bool:
    """Returns True if the user's UPN domain is in the allowed list.

    allowed_domains: comma-separated, e.g. "xenpool.de,xenpool.local".
    Empty string = allow any domain.
    """
    if not allowed_domains.strip():
        return True
    domains = [d.strip().lower() for d in allowed_domains.split(",") if d.strip()]
    if not domains:
        return True
    upn = user.get("upn") or user.get("email") or ""
    user_domain = upn.split("@")[-1].lower() if "@" in upn else ""
    return user_domain in domains


def new_state() -> str:
    """Generates a cryptographically random CSRF state token."""
    return secrets.token_urlsafe(32)
