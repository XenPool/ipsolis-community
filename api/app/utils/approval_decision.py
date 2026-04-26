"""Shared approval-decision logic.

Both the portal route (session-authenticated) and the tokenized external
route call this helper so the two paths can never drift on what counts as
"approved" or how downstream effects are dispatched.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.approval import OrderApproval
from app.models.asset import AssetType
from app.models.order import Order, OrderStatus
from app.utils.audit import _order_snap, aaudit, classify_asset_type
from app.utils.sod import is_configurer_of_asset_type

logger = logging.getLogger(__name__)


class SoDViolation(Exception):
    """Raised when an approver attempted to decide on an order whose
    asset type they configured. Routes catch and translate to HTTP 409."""

    def __init__(self, approver_email: str, asset_type_id: int, audit_excerpt: str | None) -> None:
        self.approver_email = approver_email
        self.asset_type_id = asset_type_id
        self.audit_excerpt = audit_excerpt
        super().__init__(
            f"SoD: {approver_email} configured asset type {asset_type_id} "
            f"and so cannot approve requests against it"
        )


class DecisionResult:
    __slots__ = ("status", "all_granted")

    def __init__(self, status: str, all_granted: bool) -> None:
        self.status = status              # "approved" | "declined" | "already_decided"
        self.all_granted = all_granted    # True iff this decision unblocked the order


async def apply_approval_decision(
    db: AsyncSession,
    approval: OrderApproval,
    decision: str,
    comment: str | None,
    *,
    actor: str | None = None,
) -> DecisionResult:
    """Record ``decision`` on ``approval`` and trigger downstream effects.

    Caller is responsible for verifying that the actor is authorized to
    decide on this approval (portal session match, or valid signed token).
    The function commits the session.

    ``actor`` is the audit attribution string identifying who decided —
    typically ``portal_actor_by(current_user, "decide_approval")`` for
    the session path or ``api:approval_token:<email>`` for the
    signed-token path. Falls back to a synthetic
    ``api:apply_approval_decision (approver:<email>)`` when omitted so
    legacy callers keep working.

    Returns a ``DecisionResult`` describing what happened so the caller can
    render an appropriate response.
    """
    if approval.status != "pending":
        return DecisionResult(status="already_decided", all_granted=False)

    order = await db.get(Order, approval.order_id)
    if not order:
        # Should never happen given FK; defensively roll back the partial mutation.
        logger.error("Approval %s references missing order %s", approval.id, approval.order_id)
        await db.rollback()
        return DecisionResult(status="already_decided", all_granted=False)

    # SoD: block self-approval of orders whose asset type the
    # approver configured. Runs **before** mutating the approval row
    # (so a denied SoD attempt leaves the approval `pending` for a
    # different approver). Only fires on ``approve`` — declines are
    # always allowed since "I can't approve my own work" doesn't
    # apply when the user is rejecting it.
    if decision == "approve":
        is_config, excerpt = await is_configurer_of_asset_type(
            db, order.asset_type_id, approval.approver_email,
        )
        if is_config:
            raise SoDViolation(approval.approver_email, order.asset_type_id, excerpt)

    norm = "approved" if decision == "approve" else "declined"
    approval.status = norm
    approval.decided_at = datetime.now(timezone.utc)
    approval.comment = (comment or "").strip() or None

    # Resolve the asset type once — needed for classification on every
    # audit row this decision generates and for the quorum check below.
    asset_type = await db.get(AssetType, order.asset_type_id)
    classification = classify_asset_type(asset_type)

    # Default actor when caller hasn't supplied one — preserves
    # back-compat for any external callers we haven't migrated yet.
    actor = actor or f"api:apply_approval_decision (approver:{approval.approver_email})"

    from celery import Celery
    celery_app = Celery(broker=settings.CELERY_BROKER_URL)

    if norm == "declined":
        old_order_status = order.status.value
        order.status = OrderStatus.REJECTED
        order.error_message = (
            f"Declined by {approval.approver_name}: "
            f"{approval.comment or 'no reason given'}"
        )
        # Two audit rows: the approval row's decision, and the order
        # status transition that the decline triggers.
        await aaudit(
            db, "order_approval", approval.id, "declined",
            new={
                "approver_email": approval.approver_email,
                "approver_type": approval.approver_type,
                "rule_name": approval.rule_name,
                "comment": approval.comment,
            },
            by=actor, classification=classification,
        )
        await aaudit(
            db, "order", order.id, "status_changed",
            old={"status": old_order_status},
            new={"status": OrderStatus.REJECTED.value, "reason": order.error_message},
            by=actor, classification=classification,
        )
        celery_app.send_task(
            "tasks.workflows.dynamic_runner.send_approval_result_email",
            args=[order.id, False, approval.approver_name, approval.comment],
            queue="provision",
        )
        await db.commit()
        logger.info("Approval %s declined for order %s", approval.id, order.id)
        return DecisionResult(status="declined", all_granted=False)

    # Approved branch — emit the per-approval audit row immediately so
    # the trail captures each decision even when the quorum isn't
    # yet met. The order-status transition (PENDING → DELIVERED-path)
    # gets its own row inside the threshold-met branch below.
    await aaudit(
        db, "order_approval", approval.id, "approved",
        new={
            "approver_email": approval.approver_email,
            "approver_type": approval.approver_type,
            "rule_name": approval.rule_name,
            "comment": approval.comment,
        },
        by=actor, classification=classification,
    )

    # Approved — check whether quorum is now satisfied. Slice 2 of the
    # rules engine introduces per-rule N-of-M: each rule with its own
    # ``min_approvals_required`` (frozen onto each ``OrderApproval`` as
    # ``rule_threshold``) forms a private quorum group. All other
    # approvers — manager, owner, and rule-driven approvers without a
    # per-rule threshold — fold into a single "global" group governed
    # by ``asset_type.min_approvals_required``. The order is only
    # unblocked when *every* group has met its threshold.
    rows = await db.execute(
        select(OrderApproval).where(OrderApproval.order_id == order.id)
    )
    all_approvals = list(rows.scalars().all())

    global_threshold_cfg = (asset_type.min_approvals_required if asset_type else None) or 0

    # Bucket approvals: "global" plus one bucket per rule_name with a
    # rule_threshold. Approvals from the same rule but without a
    # threshold continue to live in "global".
    buckets: dict[str, list[OrderApproval]] = {"global": []}
    bucket_thresholds: dict[str, int] = {}
    for a in all_approvals:
        if a.rule_threshold and a.rule_name:
            key = f"rule:{a.rule_name}"
            buckets.setdefault(key, []).append(a)
            # All approvals from the same rule carry the same threshold;
            # take the first non-null we see and ignore drift.
            bucket_thresholds.setdefault(key, int(a.rule_threshold))
        else:
            buckets["global"].append(a)

    # Apply legacy "0/NULL/>=total → all required" coercion to global.
    global_total = len(buckets["global"])
    if global_threshold_cfg <= 0 or global_threshold_cfg >= global_total:
        bucket_thresholds["global"] = global_total
        global_mode = "all"
    else:
        bucket_thresholds["global"] = global_threshold_cfg
        global_mode = f"{global_threshold_cfg}-of-{global_total}"

    # Per-rule buckets: clamp to the bucket size so a rule that asks
    # for more approvers than it has doesn't create an unfulfillable
    # quorum (== "all of this rule's approvers").
    for key, members in buckets.items():
        if key == "global":
            continue
        bucket_thresholds[key] = min(bucket_thresholds[key], len(members))

    bucket_met: dict[str, bool] = {}
    bucket_progress: dict[str, str] = {}
    for key, members in buckets.items():
        if not members:
            bucket_met[key] = True
            continue
        approved = sum(1 for a in members if a.status == "approved")
        bucket_met[key] = approved >= bucket_thresholds[key]
        bucket_progress[key] = f"{approved}/{bucket_thresholds[key]}"

    threshold_met = all(bucket_met.values())
    approved_count = sum(1 for a in all_approvals if a.status == "approved")
    mode = ", ".join(
        f"{key}={bucket_progress[key]}"
        for key in sorted(buckets)
        if buckets[key]
    ) or global_mode

    if threshold_met:
        # Mark remaining pending approvals as "superseded" so they
        # disappear from pending lists, no longer attract reminders /
        # escalations, and can't be acted on retroactively. Reuse
        # ``decided_at`` for the supersession timestamp.
        now = datetime.now(timezone.utc)
        superseded = 0
        old_order_status = order.status.value
        for a in all_approvals:
            if a.status == "pending":
                a.status = "superseded"
                a.decided_at = now
                superseded += 1

        # Local import — _post_approval_dispatch lives in the portal route module
        # so the side-effects (asset reservation, runbook dispatch) stay there.
        from app.routes.portal import _post_approval_dispatch
        await _post_approval_dispatch(order, db, celery_app)
        # Capture the post-dispatch status (typically PROCESSING / SCHEDULED).
        # Single audit row covers both the gate-clearance and the
        # downstream status hand-off; the per-approval audit rows above
        # already record who voted to release the gate.
        await aaudit(
            db, "order", order.id, "approved_and_dispatched",
            old={"status": old_order_status},
            new=_order_snap(order) | {
                "quorum": mode,
                "superseded_pending": superseded,
            },
            by=actor, classification=classification,
        )
        celery_app.send_task(
            "tasks.workflows.dynamic_runner.send_approval_result_email",
            args=[order.id, True],
            queue="provision",
        )
        logger.info(
            "Order %s approval threshold met (%s, %d approved, %d superseded) — dispatching",
            order.id, mode, approved_count, superseded,
        )
    else:
        logger.info(
            "Order %s approval %d approved (%s) — still waiting",
            order.id, approved_count, mode,
        )

    await db.commit()
    return DecisionResult(status="approved", all_granted=threshold_met)
