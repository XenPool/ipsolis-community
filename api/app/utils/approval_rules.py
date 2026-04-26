"""Conditional approval rules evaluator.

Asset definitions can declare a list of rules of shape::

    {
      "name":      "Long extension needs CISO",
      "condition": {"field": "duration_days", "op": ">",  "value": 90},
      "approvers": [{"email": "ciso@example.com", "name": "CISO"}]
    }

Slice 1 supports six operators (``>``, ``>=``, ``<``, ``<=``, ``==``,
``contains``) and six condition fields (``duration_days``,
``monthly_cost``, ``has_pii``, ``has_phi``, ``has_pci``,
``requester_department``). Boolean composition (AND/OR) and richer
field types are deliberately deferred — the evaluator centralises the
matching logic so it can grow without touching call sites.

The evaluator never raises on malformed rules; a bad rule is logged
and skipped so a hand-edited JSON typo can't block order creation.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


SUPPORTED_FIELDS = (
    "duration_days",
    "monthly_cost",
    "has_pii",
    "has_phi",
    "has_pci",
    "requester_department",
)
SUPPORTED_OPS = (">", ">=", "<", "<=", "==", "contains")


def build_context(order: Any, asset_type: Any) -> dict[str, Any]:
    """Build the dict the condition expressions evaluate against.

    ``order`` is an ``Order`` ORM row in the middle of being created
    (its dates and requester snapshot are populated, the row may not be
    flushed yet); ``asset_type`` is its ``AssetType`` parent.
    """
    duration_days = 0
    if order.requested_from and order.requested_until:
        try:
            duration_days = (order.requested_until - order.requested_from).days
        except Exception:  # noqa: BLE001 — defensive; bad dates shouldn't crash here
            duration_days = 0

    monthly_cost = 0.0
    if asset_type.monthly_cost is not None:
        try:
            monthly_cost = float(asset_type.monthly_cost)
        except (TypeError, ValueError):
            monthly_cost = 0.0

    has = {"pii": False, "phi": False, "pci": False}
    for attr in (asset_type.config or []):
        cls = (attr.get("classification") or "").lower()
        if cls in has:
            has[cls] = True

    return {
        "duration_days": duration_days,
        "monthly_cost": monthly_cost,
        "has_pii": has["pii"],
        "has_phi": has["phi"],
        "has_pci": has["pci"],
        "requester_department": (order.requester_department or "").strip(),
    }


def _matches(condition: dict[str, Any] | None, context: dict[str, Any]) -> bool:
    if not isinstance(condition, dict):
        return False
    field = condition.get("field")
    op = condition.get("op")
    expected = condition.get("value")
    if field not in SUPPORTED_FIELDS or op not in SUPPORTED_OPS:
        logger.warning("approval_rules: skipping condition with unknown field=%r op=%r", field, op)
        return False
    actual = context.get(field)

    # Numeric ops — coerce both sides
    if op in (">", ">=", "<", "<="):
        try:
            a = float(actual)
            e = float(expected)
        except (TypeError, ValueError):
            return False
        if op == ">":  return a > e
        if op == ">=": return a >= e
        if op == "<":  return a < e
        if op == "<=": return a <= e

    # Equality and substring — string compare, case-insensitive.
    # Booleans serialise as "true"/"false" so a rule with value: true
    # and value: "true" both work.
    actual_s = str(actual).lower() if actual is not None else ""
    expected_s = str(expected).lower() if expected is not None else ""
    if op == "==":       return actual_s == expected_s
    if op == "contains": return expected_s in actual_s
    return False


def evaluate_rules(
    rules: list[dict[str, Any]] | None,
    context: dict[str, Any],
) -> list[dict[str, str]]:
    """Walk ``rules``, return the merged list of approver dicts to add.

    Each returned approver carries ``rule_name`` so the audit trail
    (and future UI) can show which rule triggered the inclusion.
    De-dups by lowercased email so two rules naming the same approver
    don't create two approval rows.
    """
    matched: list[dict[str, str]] = []
    seen_emails: set[str] = set()
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not _matches(rule.get("condition"), context):
            continue
        rule_name = (rule.get("name") or "rule").strip()
        for approver in rule.get("approvers") or []:
            if not isinstance(approver, dict):
                continue
            email = (approver.get("email") or "").strip()
            if not email:
                continue
            key = email.lower()
            if key in seen_emails:
                continue
            seen_emails.add(key)
            matched.append({
                "email": email,
                "name": (approver.get("name") or email).strip(),
                "rule_name": rule_name,
            })
            logger.info("approval_rules: rule %r matched → adding %s", rule_name, email)
    return matched
