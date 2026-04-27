"""Setup state for the dashboard checklist.

Each item answers "is this part of the platform configured yet?".
The checklist is shown on the admin dashboard so operators can see at
a glance what's left during initial bring-up, and so a quick health
check during ongoing operation surfaces any regressions
(e.g. someone deleted the only asset definition).

Items are derived live from the DB on every request — no flag table.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.api_token import ApiToken
from app.models.asset import AssetPool, AssetType
from app.models.config import AppConfig
from app.utils.auth import require_admin_key
from app.utils.rbac import require_role

router = APIRouter(
    prefix="/admin/setup",
    tags=["admin-setup"],
    # Initial-setup endpoints provision integrations and the first API
    # token — superadmin only, since they touch infrastructure-level
    # state that wouldn't normally be re-applied after deployment.
    dependencies=[Depends(require_admin_key), require_role("superadmin")],
)


async def _get_value(db: AsyncSession, key: str) -> str:
    row = await db.execute(select(AppConfig.value).where(AppConfig.key == key))
    return (row.scalar_one_or_none() or "").strip()


async def _has_any(db: AsyncSession, model) -> bool:
    row = await db.execute(select(func.count()).select_from(model))
    return (row.scalar_one() or 0) > 0


@router.get("/state")
async def setup_state(db: AsyncSession = Depends(get_db)) -> dict:
    """Return the setup checklist as JSON. The dashboard card renders this.

    Each item: ``{key, label, done, hint, link}``.
    ``done`` is a boolean derived from current DB state.
    ``link`` points the admin at the page where they fix the gap.
    """
    # Cheap reads — all indexed lookups, all <1 ms in normal install sizes.
    app_title = await _get_value(db, "app.title")
    app_logo = await _get_value(db, "app.logo")

    smtp_server = await _get_value(db, "email.smtp_server")
    smtp_from = await _get_value(db, "email.from")

    ad_server = await _get_value(db, "ad.server")

    entra_mode = (await _get_value(db, "entra.mode")) or "disabled"
    entra_client_id = await _get_value(db, "entra.client_id")

    teams_mode = (await _get_value(db, "teams.mode")) or "disabled"
    teams_webhook = await _get_value(db, "teams.webhook_url")

    siem_enabled = (await _get_value(db, "siem.enabled") or "false").lower() == "true"
    siem_endpoint = await _get_value(db, "siem.endpoint_url")

    asset_types_count_row = await db.execute(select(func.count()).select_from(AssetType))
    asset_types_count = asset_types_count_row.scalar_one()

    pool_count_row = await db.execute(select(func.count()).select_from(AssetPool))
    pool_count = pool_count_row.scalar_one()

    # "Has at least one bearer token (besides admin:* legacy fallback)"
    token_rows = await db.execute(
        select(func.count()).select_from(ApiToken).where(ApiToken.revoked_at.is_(None))
    )
    has_token = (token_rows.scalar_one() or 0) > 0

    items = [
        {
            "key": "branding",
            "label": "Set application title and logo",
            "done": bool(app_title and app_title != "ip·Solis") or bool(app_logo),
            "hint": "Customise the portal so users recognise your tenant's branding.",
            "link": "/ui/settings#general",
            "tier": "essential",
        },
        {
            "key": "email",
            "label": "Configure SMTP for notifications",
            "done": bool(smtp_server and smtp_from),
            "hint": "Approval requests, expiry reminders, and alerts go via SMTP.",
            "link": "/ui/settings#email",
            "tier": "essential",
        },
        {
            "key": "ad",
            "label": "Connect to Active Directory",
            "done": bool(ad_server),
            "hint": "Required for user lookup, manager resolution, and group membership checks.",
            "link": "/ui/settings#ad",
            "tier": "essential",
        },
        {
            "key": "entra",
            "label": "Enable portal SSO via Entra ID",
            "done": entra_mode != "disabled" and bool(entra_client_id),
            "hint": "End users log into the self-service portal with their Entra ID account.",
            "link": "/ui/settings#ad",
            "tier": "essential",
        },
        {
            "key": "asset_types",
            "label": "Create your first asset definition",
            "done": asset_types_count > 0,
            "hint": "Asset definitions describe what users can request (VDI, SaaS license, …).",
            "link": "/ui/asset-types",
            "tier": "essential",
        },
        {
            "key": "asset_pool",
            "label": "Add at least one asset to the pool",
            "done": pool_count > 0,
            "hint": "Required for personal and shared assignment models. Skip for pure capacity-pooled types.",
            "link": "/ui/asset-pool",
            "tier": "essential",
        },
        {
            "key": "teams",
            "label": "Enable Microsoft Teams approval cards",
            "done": teams_mode == "enabled" and bool(teams_webhook),
            "hint": "Approvers get an Adaptive Card with a one-click review link in addition to email.",
            "link": "/ui/settings#email",
            "tier": "recommended",
        },
        {
            "key": "siem",
            "label": "Stream the audit log to a SIEM",
            "done": siem_enabled and bool(siem_endpoint),
            "hint": "Forwards every audit_log row to Splunk HEC for tamper-evident retention outside the app.",
            "link": "/ui/settings#compliance",
            "tier": "recommended",
        },
        {
            "key": "api_token",
            "label": "Issue a per-integration API token",
            "done": has_token,
            "hint": "Replaces the shared X-Admin-Key with revocable, scoped bearer tokens for ServiceNow / scripts / Prometheus.",
            "link": "/ui/api-tokens",
            "tier": "recommended",
        },
    ]

    essential = [i for i in items if i["tier"] == "essential"]
    essential_done = sum(1 for i in essential if i["done"])
    essential_total = len(essential)
    recommended = [i for i in items if i["tier"] == "recommended"]
    recommended_done = sum(1 for i in recommended if i["done"])
    recommended_total = len(recommended)

    return {
        "items": items,
        "essential": {"done": essential_done, "total": essential_total},
        "recommended": {"done": recommended_done, "total": recommended_total},
        "complete": essential_done == essential_total and recommended_done == recommended_total,
    }
