"""Per-classification approval routing.

Inspects an asset type's attribute classifications, consults the
``approval.classification_policy.*`` config keys, and returns the
list of approver dicts to inject — or an empty list when no
classification on the asset type is configured for auto-routing.

The conditional approval rules engine in ``approval_rules.py`` already
supports the ``has_pii / has_phi / has_pci`` context fields for
operators who want fine-grained logic. This helper is the *defaults*
path: a one-click toggle that fires regardless of whether rules are
configured, de-duped against the manager / owner / rule approvers
on the calling side.

Two policy values per class:

* ``compliance_officer`` — auto-add ONE step pointing at the global
  ``approval.compliance_officer_email`` contact. Standard for
  centralised compliance teams (one inbox handles every regulated
  request, regardless of which asset type).

* ``owner_of_record`` — auto-add one step PER entry in the asset
  type's ``approval_owners`` list. Standard for HIPAA-style
  workflows where the data steward who actually owns the PHI
  surface must sign off, not a generic compliance team. The
  approver_type is ``owner_of_record`` (distinct from the static
  ``application_owner`` flag) so audit-log queries can tell whether
  an owner step was triggered by the static toggle or by a
  classification policy.

Activation precedence: PCI > PHI > PII (strictest wins). Even when
multiple classes fire, only one *policy* is applied — the one bound
to the strictest matching class. The number of approval rows added
depends on which policy fires:

* ``compliance_officer`` always emits one row.
* ``owner_of_record`` emits N rows, one per entry in
  ``asset_type.approval_owners``. An asset type with no owners
  configured silently skips the step (logged at INFO so operators
  can debug).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import AppConfig

logger = logging.getLogger(__name__)

# Stricter classes first so the precedence order maps directly to
# iteration order. The settings UI presents them in the same order,
# which makes the "PCI is the strictest, PII is the most permissive"
# mental model obvious.
_CLASSIFICATIONS_STRICT_FIRST = ("pci", "phi", "pii")


async def load_classification_policy(db: AsyncSession) -> dict[str, str]:
    """Read ``approval.*`` config keys relevant to this routing pass.

    Returns a flat dict with keys ``pii`` / ``phi`` / ``pci`` /
    ``compliance_officer_email`` / ``compliance_officer_name`` —
    same shape regardless of how many keys are actually present in
    the database. Missing rows fall back to ``none`` / empty.
    """
    rows = await db.execute(
        select(AppConfig.key, AppConfig.value).where(
            AppConfig.key.in_([
                "approval.classification_policy.pii",
                "approval.classification_policy.phi",
                "approval.classification_policy.pci",
                "approval.compliance_officer_email",
                "approval.compliance_officer_name",
            ])
        )
    )
    raw = {key: (value or "").strip() for key, value in rows.all()}
    return {
        "pii": (raw.get("approval.classification_policy.pii") or "none").lower(),
        "phi": (raw.get("approval.classification_policy.phi") or "none").lower(),
        "pci": (raw.get("approval.classification_policy.pci") or "none").lower(),
        "compliance_officer_email": raw.get("approval.compliance_officer_email", ""),
        "compliance_officer_name": (
            raw.get("approval.compliance_officer_name") or "Compliance Officer"
        ),
    }


def asset_type_classifications(asset_type: Any) -> set[str]:
    """Return the set of classes ({'pii', 'phi', 'pci'}) any of the
    asset type's attributes carry. Empty set means "internal only" —
    no auto-routing applies."""
    present: set[str] = set()
    for attr in (asset_type.config or []):
        cls = (attr.get("classification") or "").lower()
        if cls in _CLASSIFICATIONS_STRICT_FIRST:
            present.add(cls)
    return present


def classification_approvers(
    asset_type: Any,
    policy: dict[str, str],
) -> list[dict[str, str]]:
    """Decide which approver row(s) the classification policy should inject.

    Returns a list of approver dicts (possibly empty). Each entry:

    * ``email``         — recipient
    * ``name``          — display name (falls back to email)
    * ``trigger_class`` — strictest classification on the asset type
                          that matched a non-``none`` policy
    * ``policy``        — ``compliance_officer`` or ``owner_of_record``;
                          drives the ``approver_type`` on the persisted
                          row at the call site

    Empty list when no classification on the asset type matches a
    non-``none`` policy, or when the policy fires but the resolved
    target list is empty (compliance_officer mode with no email,
    owner_of_record mode with no ``approval_owners`` on the asset
    type) — both cases log a hint at INFO from the call site so
    operators can debug a "policy is set but no extra step appeared"
    surprise.

    Strictest-first iteration: when an asset type carries both PII
    and PHI fields and both classes have non-``none`` policies, the
    PHI policy wins (PHI > PII in ``_CLASSIFICATIONS_STRICT_FIRST``).
    Only one policy fires per order.
    """
    present = asset_type_classifications(asset_type)
    if not present:
        return []

    for cls in _CLASSIFICATIONS_STRICT_FIRST:
        if cls not in present:
            continue
        chosen = (policy.get(cls) or "none").lower()
        if chosen == "compliance_officer":
            email = policy.get("compliance_officer_email", "").strip()
            if not email:
                logger.info(
                    "classification_routing: policy=%s for class=%s but "
                    "compliance_officer_email is empty — skipping auto-step",
                    chosen, cls,
                )
                return []
            return [{
                "email": email,
                "name": policy.get("compliance_officer_name") or email,
                "trigger_class": cls,
                "policy": "compliance_officer",
            }]
        if chosen == "owner_of_record":
            owners = list(getattr(asset_type, "approval_owners", None) or [])
            if not owners:
                logger.info(
                    "classification_routing: policy=%s for class=%s but "
                    "asset type has no approval_owners — skipping auto-step",
                    chosen, cls,
                )
                return []
            out: list[dict[str, str]] = []
            seen: set[str] = set()
            for owner in owners:
                if not isinstance(owner, dict):
                    continue
                email = (owner.get("email") or "").strip()
                if not email:
                    continue
                key = email.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "email": email,
                    "name": (owner.get("name") or email).strip(),
                    "trigger_class": cls,
                    "policy": "owner_of_record",
                })
            return out
        # ``none`` or any unknown value — no auto-step from this
        # class; fall through to the next less-strict class. The
        # existing "strictest CONFIGURED wins" contract: when the
        # asset type carries PCI + PHI fields but only PHI is set
        # to a non-none policy, the PHI policy applies even though
        # PCI is technically the strictest class present.
        continue
    return []
