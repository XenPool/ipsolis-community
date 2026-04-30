"""Seed per-classification approval routing config keys.

Operators in regulated industries want a one-click toggle: "any order
that touches PII / PHI / PCI fields automatically goes through a
compliance-officer approval step." The conditional-approval-rules
engine already supports this via the ``has_pii / has_phi / has_pci``
context fields, but only when an admin writes the matching rules
explicitly. This migration adds the **default-driven** path: three
classification policy keys plus the compliance officer's contact info,
applied automatically at order creation.

Policy values per classification:

* ``none`` (default) — no auto-injected approval step. Existing
  conditional rules / static manager / app-owner toggles still apply.
* ``compliance_officer`` — inject a ``compliance_officer`` approval row
  pointing at ``approval.compliance_officer_email``. De-duped against
  the manager / owner / rule approvers, so an officer who is also the
  manager doesn't get two approval rows.

Activation precedence: PCI > PHI > PII (strictest wins). An order
whose asset type carries both PHI and PII fields where both classes
are configured for ``compliance_officer`` adds *one* compliance step
(not two). The rule evaluator keeps the same de-dup-by-email
contract — a compliance officer who's also named in a rule still
gets just one row.

Revision ID: 0091
Revises: 0090
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0091"
down_revision: Union[str, None] = "0090"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KEYS = [
    (
        "approval.classification_policy.pii",
        "none",
        "Per-classification approval policy for PII-bearing orders. "
        "One of 'none' (default — leave existing approval flow alone) or "
        "'compliance_officer' (auto-add an approval step pointing at "
        "approval.compliance_officer_email when the order's asset type "
        "has any attribute classified as 'pii'). Conditional rules and "
        "static manager / owner toggles still apply on top.",
        False,
    ),
    (
        "approval.classification_policy.phi",
        "none",
        "Per-classification approval policy for PHI-bearing orders. "
        "Same options as approval.classification_policy.pii. PHI is "
        "stricter than PII; a deployment under HIPAA / equivalent will "
        "typically set this to 'compliance_officer' even if PII stays "
        "at 'none'.",
        False,
    ),
    (
        "approval.classification_policy.pci",
        "none",
        "Per-classification approval policy for PCI-bearing orders. "
        "Same options as approval.classification_policy.pii. PCI is "
        "the strictest tier — PCI DSS controls typically mandate a "
        "documented approval trail for any access to cardholder data, "
        "so a 'compliance_officer' setting here is common.",
        False,
    ),
    (
        "approval.compliance_officer_email",
        "",
        "Email address that receives compliance-officer approval "
        "requests when any approval.classification_policy.* is set to "
        "'compliance_officer'. Single email — group lists work too if "
        "the receiving inbox routes to a team. The same address is "
        "consulted regardless of which classification triggered the "
        "step (PII / PHI / PCI all share one compliance officer).",
        False,
    ),
    (
        "approval.compliance_officer_name",
        "Compliance Officer",
        "Display name for the compliance officer in approval emails "
        "and the portal's pending-approval page. Falls back to the "
        "email if left empty.",
        False,
    ),
]


def upgrade() -> None:
    for key, value, description, is_secret in _KEYS:
        op.execute(
            f"""
            INSERT INTO app_config (key, value, description, is_secret)
            VALUES ({_lit(key)}, {_lit(value)}, {_lit(description)}, {str(is_secret).lower()})
            ON CONFLICT (key) DO NOTHING
            """
        )


def downgrade() -> None:
    for key, _, _, _ in _KEYS:
        op.execute(f"DELETE FROM app_config WHERE key = {_lit(key)}")


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
