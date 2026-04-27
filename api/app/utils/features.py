"""Feature flag registry and FastAPI dependency for Enterprise gating.

Feature keys come from ``EDITIONS.md`` (``ENTERPRISE_FEATURES`` section). Any
change to that document should be mirrored here.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.utils.license import is_feature_enabled

# Canonical list of Enterprise features. Keys used throughout routes/templates.
# Value is a human-readable short name for the 403 response and logs.
ENTERPRISE_FEATURES: dict[str, str] = {
    "standalone_runbooks":      "Standalone Runbooks",
    "visual_runbook_builder":   "Visual Runbook Builder",
    "ps_module_management":     "PowerShell Module Management",
    "deputy_support":           "Deputy Support",
    "scheduled_orders":         "Scheduled Orders",
    "app_owner_approval":       "Application Owner Approval",
    "reapproval_on_modify":     "Re-approval on Modify",
    "servicenow_webhook":       "ServiceNow Webhook",
    "vsphere_integration":      "VMware vSphere Integration",
    "xenserver_integration":    "XenServer / XCP-ng Integration",
    "sccm_integration":         "SCCM Integration",
    "eligible_requestors":      "Eligible Requestors",
    "email_template_editor":    "Email Template Editor",
    "app_branding":             "App Branding",
    "global_variables":         "Global Variables",
    "audit_log_viewer":         "Audit Log Viewer",
    "change_log_viewer":        "Order Change Log Viewer",
    "advanced_maintenance":     "Advanced Maintenance",
    "custom_deprovision":       "Custom Deprovision Policy",
    # ── RBAC compliance extensions ─────────────────────────────────────
    # Community ships the core role ladder + per-user accounts free of
    # charge. Enterprise adds the auditor-grade extensions: scoped
    # grants, role-bound integration tokens, separation-of-duties
    # enforcement, and (slice-4 backlog) password rotation policies.
    "rbac_asset_type_grants":   "Per-Asset-Type ACL Grants",
    "rbac_token_role_binding":  "Role-Bound API Tokens",
    "rbac_sod_enforcement":     "Separation-of-Duties Enforcement",
    "password_policy":          "Password Rotation & Lockout Policy",
}


def _enterprise_error(feature: str) -> HTTPException:
    label = ENTERPRISE_FEATURES.get(feature, feature)
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"{label} requires an ip·Solis Enterprise license. "
            f"Contact info@xenpool.com for licensing options."
        ),
    )


def require_enterprise(feature: str):
    """FastAPI dependency factory. Raises 403 if the feature is not licensed.

    Usage:
        @router.get("/thing", dependencies=[require_enterprise("standalone_runbooks")])
        async def endpoint(): ...

        # or per-router
        router = APIRouter(
            prefix="/admin/standalone-runbooks",
            dependencies=[Depends(require_admin_key), require_enterprise("standalone_runbooks")],
        )
    """
    async def _check() -> None:
        if not is_feature_enabled(feature):
            raise _enterprise_error(feature)

    return Depends(_check)
