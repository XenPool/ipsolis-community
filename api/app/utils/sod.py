"""Separation-of-duties (SoD) checks — RBAC slice 3.

The procurement-asked rule: a user who **configured** an asset type
(created, updated, or cloned the row) must not also **approve** access
requests for it. The check fires at decision time, blocking the
approve action with a 409 — leaves the approval row in
``pending`` so a different approver can handle it.

Configurer detection walks the ``audit_log`` for
``entity_type = 'asset_type'`` AND ``entity_id = <type_id>`` AND
``action IN ('created', 'updated', 'cloned')`` AND the
``triggered_by`` string contains the actor's identity.

Identity matching is **fuzzy by design** — the audit attribution
string format (``api:<route_label> (admin:session:<username>:<role>)``,
``portal:user:<email>``, ``token:<name>``, …) doesn't normalise the
user's "primary identifier" to a single field. We accept any of:

* approver email exactly (``ciso@example.com``)
* email local-part (``ciso``)
* admin user username if one matches the email's local-part

Cases left to slice 4:

* SoD on per-rule approvers (today only manager / owner / rule
  approvers all share the same enforcement; no per-approver-type
  carve-outs).
* Token-driven configurations are matched on ``token:<name>``; if
  an approver's email happens to match a token name SoD will
  fire. Acceptable today since token names typically don't look
  like email addresses.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


_CONFIG_ACTIONS = ("created", "updated", "cloned")


def _email_local_part(email: str) -> str:
    return email.split("@", 1)[0].strip().lower() if "@" in email else email.strip().lower()


async def is_configurer_of_asset_type(
    db: AsyncSession,
    asset_type_id: int,
    approver_email: str,
) -> tuple[bool, str | None]:
    """Return ``(matched, audit_excerpt)`` — True if ``approver_email``
    appears as the actor on any ``asset_type`` create/update/clone row.

    The second element is the most recent matching ``triggered_by``
    string so the SoD-block error can quote it back at the operator
    ("Blocked: you configured this asset type as
    ``admin:session:alice:admin``").
    """
    email = (approver_email or "").strip().lower()
    if not email:
        return False, None

    needles: set[str] = {email}
    local = _email_local_part(email)
    if local and local != email:
        needles.add(local)

    # Build a single SQL query matching any needle anywhere in
    # triggered_by. Postgres ILIKE is case-insensitive; the actor
    # strings we built (``portal_actor_by`` / ``actor_by``) lower-case
    # email parts so case folding is mostly cosmetic but harmless.
    rows = await db.execute(
        select(AuditLog.triggered_by, AuditLog.id, AuditLog.action)
        .where(
            AuditLog.entity_type == "asset_type",
            AuditLog.entity_id == asset_type_id,
            AuditLog.action.in_(_CONFIG_ACTIONS),
        )
        .order_by(AuditLog.id.desc())
    )
    for trig, _aid, _action in rows.all():
        trig_lc = (trig or "").lower()
        for needle in needles:
            if needle in trig_lc:
                return True, trig
    return False, None
