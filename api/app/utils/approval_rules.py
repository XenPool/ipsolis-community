"""Conditional approval rules evaluator.

Asset types declare a list of rules; each rule has a condition tree and
a list of approvers. At order creation, every rule whose condition
matches contributes its approvers to the order — on top of (and merged
with) the static manager / owner toggles.

Rule shape::

    {
      "name":      "Long extension needs CISO",
      "condition": <condition>,
      "approvers": [{"email": "ciso@example.com", "name": "CISO"}],
      "min_approvals_required": 2     // optional — see "Per-rule N-of-M" below
    }

Condition tree
--------------

A condition is either a **leaf** or a **compound** node.

Leaf::

    {"field": "<field>", "op": "<op>", "value": <value>}

Compound::

    {"op": "and"|"or"|"not", "clauses": [<condition>, ...]}

* ``and`` matches when every clause matches (empty clauses → ``True``).
* ``or``  matches when any clause matches (empty clauses → ``False``).
* ``not`` matches when its single clause does not.

Compound nodes nest freely up to ``_MAX_DEPTH``; the limit exists so a
hand-edited JSON with a recursive structure can't exhaust the stack.

Built-in leaf fields (always present in the context):

* ``duration_days`` — requested duration of the order
* ``monthly_cost`` — asset type's monthly cost
* ``has_pii`` / ``has_phi`` / ``has_pci`` — true if any attribute on the
  asset type carries the matching classification
* ``requester_department`` — AD-resolved department of the requester

Custom-attribute leaf fields (slice 2): any ``attr.<key>`` where
``<key>`` matches an attribute defined on the asset type's ``config``.
Resolved from ``order.config[key]`` at evaluation time. Values flow
through the same numeric / equality / contains operators as built-ins.

Operators::

    >, >=, <, <=, ==, contains

Per-rule N-of-M (slice 2)
-------------------------

The asset type carries a global ``min_approvals_required`` covering all
approvers (manager + owner + every rule's approvers) as one quorum.
Rules can override that for their *own* approvers by setting
``min_approvals_required`` on the rule itself. The decision evaluator
treats each such rule as its own quorum group.

The evaluator never raises on malformed rules; a bad rule is logged
and skipped so a hand-edited JSON typo can't block order creation.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


_BUILTIN_FIELDS = (
    "duration_days",
    "monthly_cost",
    "has_pii",
    "has_phi",
    "has_pci",
    "requester_department",
)
SUPPORTED_OPS = (">", ">=", "<", "<=", "==", "contains")
COMPOUND_OPS = ("and", "or", "not")

# Recursion guard for compound conditions. 8 levels of nesting is more
# than any human-built rule would ever need; bumping it costs nothing
# but rejecting unbounded JSON costs everything.
_MAX_DEPTH = 8


def build_context(order: Any, asset_type: Any) -> dict[str, Any]:
    """Build the dict the condition expressions evaluate against.

    ``order`` is an ``Order`` ORM row in the middle of being created
    (its dates, requester snapshot, and ``config`` are populated; the
    row may not yet be flushed); ``asset_type`` is its ``AssetType``
    parent.

    Custom-attribute values (``order.config``) flow into the context as
    ``attr.<key>`` so a rule can reference e.g. ``attr.justification``
    or ``attr.requested_cores`` directly.
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

    ctx: dict[str, Any] = {
        "duration_days": duration_days,
        "monthly_cost": monthly_cost,
        "has_pii": has["pii"],
        "has_phi": has["phi"],
        "has_pci": has["pci"],
        "requester_department": (order.requester_department or "").strip(),
    }

    # Slice 2: order config attributes flat-mapped under attr.<key>. The
    # key set is whatever the order actually carries; rules referencing
    # attrs that weren't filled in will see ``None`` (= empty string
    # under string compare, = comparison failure under numeric ops),
    # which is the right semantic — a missing field never matches.
    order_config = order.config if isinstance(order.config, dict) else {}
    for key, value in order_config.items():
        ctx[f"attr.{key}"] = value

    return ctx


def _eval_leaf(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    field = condition.get("field")
    op = condition.get("op")
    expected = condition.get("value")

    if not isinstance(field, str) or op not in SUPPORTED_OPS:
        logger.warning("approval_rules: skipping leaf with unknown field=%r op=%r", field, op)
        return False
    # Built-in fields use the strict allowlist; attr.* fields skip it.
    if not field.startswith("attr.") and field not in _BUILTIN_FIELDS:
        logger.warning("approval_rules: skipping leaf with unknown field=%r", field)
        return False

    actual = context.get(field)

    # Numeric ops — coerce both sides; NULL / non-numeric → no match.
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

    # Equality and substring — case-insensitive string compare. Booleans
    # serialise as "true"/"false" so a rule with value: true matches
    # both Python True and the string "true". For multi-valued attrs
    # (lists) ``contains`` checks membership against any list element.
    if op == "contains" and isinstance(actual, (list, tuple)):
        expected_s = str(expected).lower() if expected is not None else ""
        for item in actual:
            if expected_s in str(item).lower():
                return True
        return False

    actual_s = str(actual).lower() if actual is not None else ""
    expected_s = str(expected).lower() if expected is not None else ""
    if op == "==":       return actual_s == expected_s
    if op == "contains": return expected_s in actual_s
    return False


def _eval_compound(condition: dict[str, Any], context: dict[str, Any], depth: int) -> bool:
    op = condition.get("op")
    clauses = condition.get("clauses")
    if not isinstance(clauses, list):
        logger.warning("approval_rules: compound %r missing clauses list", op)
        return False
    if op == "not":
        if len(clauses) != 1:
            logger.warning("approval_rules: 'not' must have exactly one clause, got %d", len(clauses))
            return False
        return not _eval_condition(clauses[0], context, depth + 1)
    if op == "and":
        # Empty AND is vacuously True. Caller can use this to write a
        # rule that always fires (combined with attr-only conditions).
        return all(_eval_condition(c, context, depth + 1) for c in clauses)
    if op == "or":
        # Empty OR is False — "no clauses match" is the safer default.
        return any(_eval_condition(c, context, depth + 1) for c in clauses)
    logger.warning("approval_rules: unknown compound op %r", op)
    return False


def _eval_condition(condition: Any, context: dict[str, Any], depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        logger.warning("approval_rules: condition exceeded max depth %d — treating as no-match", _MAX_DEPTH)
        return False
    if not isinstance(condition, dict):
        return False
    op = condition.get("op")
    if op in COMPOUND_OPS:
        return _eval_compound(condition, context, depth)
    return _eval_leaf(condition, context)


def evaluate_rules(
    rules: list[dict[str, Any]] | None,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Walk ``rules``, return the merged list of approver dicts to add.

    Each returned approver carries:

    * ``email`` / ``name`` — the approver
    * ``rule_name`` — the name of the rule that matched
    * ``rule_threshold`` — per-rule N-of-M quorum count, or ``None`` if
      the rule has no quorum override (its approvers fold into the
      asset-type-level pool).

    De-dups by lowercased email so two rules naming the same approver
    don't create two approval rows. When the same email shows up in
    multiple rules, the first matching rule's ``rule_threshold`` wins
    — a slightly arbitrary but deterministic choice; admins who care
    about per-rule quorum should keep approver lists disjoint.
    """
    matched: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not _eval_condition(rule.get("condition"), context):
            continue
        rule_name = (rule.get("name") or "rule").strip()
        # Optional per-rule N-of-M. Coerce to int; ignore garbage.
        rule_threshold: int | None = None
        raw_thresh = rule.get("min_approvals_required")
        if raw_thresh is not None:
            try:
                t = int(raw_thresh)
                rule_threshold = t if t > 0 else None
            except (TypeError, ValueError):
                rule_threshold = None
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
                "rule_threshold": rule_threshold,
            })
            logger.info(
                "approval_rules: rule %r matched → adding %s (threshold=%s)",
                rule_name, email, rule_threshold,
            )
    return matched
