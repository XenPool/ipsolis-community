"""Per-classification approval routing.

Inspects an asset type's attribute classifications, consults the
``approval.classification_policy.*`` config keys, and returns the
compliance-officer approver dict to inject — or ``None`` when no
classification on the asset type is configured for auto-routing.

The conditional approval rules engine in ``approval_rules.py`` already
supports the ``has_pii / has_phi / has_pci`` context fields for
operators who want fine-grained logic. This helper is the *defaults*
path: a one-click toggle that fires regardless of whether rules are
configured, de-duped against the manager / owner / rule approvers
on the calling side.

Activation precedence: PCI > PHI > PII. Even when multiple classes
fire, only one compliance-officer step is added — there's a single
configured ``compliance_officer_email`` and the column-30-char
``approver_type`` value (``compliance_officer``) doesn't carry
which class triggered it. The audit row carries the full
classification of the asset type, so retention / forensic queries
can still distinguish a PHI-triggered step from a PII-triggered one.
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


def compliance_officer_approver(
    asset_type: Any,
    policy: dict[str, str],
) -> dict[str, str] | None:
    """Decide whether the order needs a compliance-officer step.

    Returns ``{"email": ..., "name": ..., "trigger_class": ...}`` when
    the asset type carries at least one classification configured
    for ``compliance_officer`` and the email is set; ``None``
    otherwise.

    ``trigger_class`` is the strictest matching class — useful for
    the audit trail / log lines so operators can see *why* the
    extra step was added.
    """
    present = asset_type_classifications(asset_type)
    if not present:
        return None
    email = policy.get("compliance_officer_email", "").strip()
    if not email:
        return None
    for cls in _CLASSIFICATIONS_STRICT_FIRST:
        if cls in present and policy.get(cls) == "compliance_officer":
            return {
                "email": email,
                "name": policy.get("compliance_officer_name") or email,
                "trigger_class": cls,
            }
    return None
