"""Async audit helper for FastAPI routes.

All writes land in the same transaction as the main change –
no separate commit needed. Entries in audit_log are immutable (no UPDATE/DELETE).
"""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


def actor_by(request: Request | None, label: str) -> str:
    """Build an audit ``triggered_by`` string from the request's actor.

    ``request.state.actor`` is populated by ``require_admin_key`` and
    ``_authenticate_webhook`` and identifies the credential used for
    the call (e.g. ``token:servicenow-int``, ``admin:session:alice``,
    ``admin:legacy_key``, ``webhook:hmac``). Wrapping it with the
    route's logical label gives auditors both *what* happened and
    *who* triggered it.

    Falls back to plain ``api:<label>`` when no actor is on state
    (unauthenticated routes), preserving back-compat.
    """
    if request is None:
        return f"api:{label}"
    actor = getattr(getattr(request, "state", None), "actor", None)
    if actor:
        return f"api:{label} ({actor})"
    return f"api:{label}"


async def aaudit(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    action: str,
    *,
    old: dict | None = None,
    new: dict | None = None,
    by: str,
    ctx: str | None = None,
) -> None:
    """Schreibt einen Audit-Log-Eintrag in die laufende Transaktion.

    Args:
        db:          Aktive AsyncSession (wird vom Caller committed)
        entity_type: "order" | "asset" | "asset_type" | "app_config"
        entity_id:   PK of the changed record
        action:      "created" | "updated" | "status_changed" | "deleted"
        old:         Snapshot before the change (None on created)
        new:         Snapshot after the change (None on deleted)
        by:          Trigger, e.g. "api:create_order" | "api:servicenow_webhook"
        ctx:         Optionaler Kontext (servicenow_ref, celery_task_id, ...)
    """
    db.add(AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        old_value=old,
        new_value=new,
        triggered_by=by,
        context=ctx,
    ))


# ── Snapshot-Helfer ────────────────────────────────────────────────────────────

def _order_snap(order) -> dict:
    return {
        "id": order.id,
        "status": order.status.value if hasattr(order.status, "value") else order.status,
        "action": order.action.value if hasattr(order.action, "value") else order.action,
        "user_email": order.user_email,
        "asset_type_id": order.asset_type_id,
        "assigned_asset_id": order.assigned_asset_id,
        "rdp_users": list(order.rdp_users or []),
        "admin_users": list(order.admin_users or []),
        "requested_until": order.requested_until.isoformat() if order.requested_until else None,
    }


def _asset_snap(asset) -> dict:
    return {
        "id": asset.id,
        "name": asset.name,
        "status": asset.status.value if hasattr(asset.status, "value") else asset.status,
        "asset_type_id": asset.asset_type_id,
        "current_order_id": asset.current_order_id,
        "expires_at": asset.expires_at.isoformat() if asset.expires_at else None,
    }


def _config_snap(cfg) -> dict:
    return {
        "id": cfg.id,
        "key": cfg.key,
        "value": "***" if cfg.is_secret else cfg.value,
        "is_secret": cfg.is_secret,
        "description": cfg.description,
    }


def _type_snap(t) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "is_active": getattr(t, "is_active", True),
        "category": t.category.value if hasattr(t.category, "value") else t.category,
        "description": t.description,
        "help_text": getattr(t, "help_text", None),
        "config": t.config,
        "assignment_model": t.assignment_model,
        "automation_mode": t.automation_mode,
        "automation_strategy": t.automation_strategy,
        "composite_steps": t.composite_steps,
        "deprovision_policy": t.deprovision_policy,
        "personal_provisioning_strategy": t.personal_provisioning_strategy,
        "naming_pattern": t.naming_pattern,
        "max_per_user": t.max_per_user,
        "min_approvals_required": getattr(t, "min_approvals_required", None),
        "monthly_cost": str(t.monthly_cost) if t.monthly_cost is not None else None,
        "currency": t.currency,
        "cost_center": t.cost_center,
    }
