"""Admin CRUD + kickoff for access certification campaigns.

Slice 1 — schema, CRUD, kickoff. Reminders, escalation, auto-revoke,
and the manager-facing portal review page are queued for slice 2.

Status semantics (campaign): ``draft`` → ``running`` (after kickoff)
→ ``closed`` (manual or all-reviews-decided) | ``cancelled`` (operator
abort).

Status semantics (review): ``pending`` → ``confirmed`` | ``revoked``
(decision recorded by reviewer / admin) | ``auto_revoked`` (slice 2 —
overdue with no decision).

The router floor is ``admin`` for writes; reads are ``auditor`` so
oversight roles can see campaign progress without being able to
mutate. Per-route gates raise the bar where appropriate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.approval import OrderApproval
from app.models.asset import AssetType
from app.models.certification import CertificationCampaign, CertificationReview
from app.models.config import AppConfig
from app.models.order import Order, OrderStatus
from app.utils.audit import _order_snap, aaudit, actor_by
from app.utils.auth import require_admin_key, require_scopes
from app.utils.certification_token import make_review_token
from app.utils.features import require_enterprise
from app.utils.rbac import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/certifications",
    tags=["admin-certifications"],
    # Enterprise-gated: access certification campaigns are an
    # ISO 27001 / SOX / PCI compliance feature.
    # Read floor: auditor+. Per-route ``require_role("admin")`` raises
    # the bar on writes. Bearer tokens still need explicit scopes —
    # ``approvals:read`` is the closest existing scope (cert reviews
    # are an approval-style workflow).
    dependencies=[
        Depends(require_admin_key),
        require_enterprise("certifications"),
        require_scopes("approvals:read"),
        require_role("auditor"),
    ],
)

_WRITE_GATE = require_role("admin")
_WRITE_SCOPE = require_scopes("approvals:write")

# Set of order statuses considered "active" for campaign scope
# selection — same set the cost report uses, plus ``provisioned`` and
# ``delivered`` since those are the orders managers actually need to
# re-confirm. Excluded: ``rejected`` / ``cancelled`` / ``failed`` /
# ``revoked`` / ``expired``.
_ACTIVE_ORDER_STATUSES = (
    "pending", "pending_approval", "scheduled",
    "processing", "provisioning", "provisioned", "delivered",
)

_VALID_CAMPAIGN_STATUSES = ("draft", "running", "closed", "cancelled")
_TERMINAL_REVIEW_STATUSES = ("confirmed", "revoked", "auto_revoked")


# ── Schemas ────────────────────────────────────────────────────────────────────

class CampaignScope(BaseModel):
    """Scope filter applied at kickoff. Empty / null fields are
    wildcards. AND across keys, OR within each list.
    """
    asset_type_ids: list[int] | None = None
    cost_centers: list[str] | None = None
    departments: list[str] | None = None
    requester_emails: list[str] | None = None


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    scope: CampaignScope = Field(default_factory=CampaignScope)
    due_at: datetime


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    scope: CampaignScope | None = None
    due_at: datetime | None = None


class ReviewDecision(BaseModel):
    decision: Literal["confirmed", "revoked"]
    comment: str | None = None


def _campaign_dict(c: CertificationCampaign, counts: dict[str, int] | None = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "scope": c.scope or {},
        "due_at": c.due_at.isoformat() if c.due_at else None,
        "status": c.status,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "closed_at": c.closed_at.isoformat() if c.closed_at else None,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "review_counts": counts or {},
    }


def _review_dict(r: CertificationReview) -> dict:
    return {
        "id": r.id,
        "campaign_id": r.campaign_id,
        "order_id": r.order_id,
        "reviewer_email": r.reviewer_email,
        "reviewer_name": r.reviewer_name,
        "status": r.status,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decided_by": r.decided_by,
        "comment": r.comment,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def _review_counts(db: AsyncSession, campaign_id: int) -> dict[str, int]:
    """Group review rows by status for dashboard tiles."""
    rows = await db.execute(
        select(CertificationReview.status, func.count(CertificationReview.id))
        .where(CertificationReview.campaign_id == campaign_id)
        .group_by(CertificationReview.status)
    )
    counts = {"pending": 0, "confirmed": 0, "revoked": 0, "auto_revoked": 0}
    for status_value, n in rows.all():
        counts[status_value] = int(n or 0)
    counts["total"] = sum(counts.values())
    return counts


# ── Campaign CRUD ──────────────────────────────────────────────────────────────

@router.get("")
async def list_campaigns(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """All campaigns, newest first, with per-status review counts."""
    result = await db.execute(
        select(CertificationCampaign).order_by(CertificationCampaign.created_at.desc())
    )
    campaigns = list(result.scalars().all())
    out: list[dict] = []
    for c in campaigns:
        counts = await _review_counts(db, c.id)
        out.append(_campaign_dict(c, counts))
    return out


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    c = await db.get(CertificationCampaign, campaign_id)
    if not c:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found",
        )
    counts = await _review_counts(db, c.id)
    return _campaign_dict(c, counts)


@router.get("/{campaign_id}/reviews")
async def list_reviews(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    c = await db.get(CertificationCampaign, campaign_id)
    if not c:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found",
        )
    rows = await db.execute(
        select(CertificationReview)
        .where(CertificationReview.campaign_id == campaign_id)
        .order_by(CertificationReview.reviewer_email, CertificationReview.id)
    )
    return [_review_dict(r) for r in rows.scalars().all()]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[_WRITE_GATE, _WRITE_SCOPE],
)
async def create_campaign(
    request: Request,
    payload: CampaignCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if payload.due_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="due_at must be in the future",
        )
    actor = actor_by(request, "create_certification_campaign")
    c = CertificationCampaign(
        name=payload.name.strip(),
        description=(payload.description or None),
        scope=payload.scope.model_dump(exclude_none=True),
        due_at=payload.due_at,
        status="draft",
        created_by=actor,
    )
    db.add(c)
    await db.flush()
    await aaudit(
        db, "certification_campaign", c.id, "created",
        new={
            "name": c.name,
            "due_at": c.due_at.isoformat(),
            "scope": c.scope,
        },
        by=actor,
    )
    await db.commit()
    await db.refresh(c)
    logger.info("certification: created campaign id=%s name=%s by=%s", c.id, c.name, actor)
    return _campaign_dict(c, await _review_counts(db, c.id))


@router.put(
    "/{campaign_id}",
    dependencies=[_WRITE_GATE, _WRITE_SCOPE],
)
async def update_campaign(
    request: Request,
    campaign_id: int,
    payload: CampaignUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = await db.get(CertificationCampaign, campaign_id)
    if not c:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found",
        )
    if c.status not in ("draft", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot edit a {c.status} campaign",
        )
    if c.status == "running":
        # Only allow editing the due date once running — name / scope edits
        # mid-cycle break the audit trail since reviews are already created.
        if payload.name is not None or payload.scope is not None or payload.description is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only due_at is editable on a running campaign",
            )

    old = {"name": c.name, "scope": c.scope, "due_at": c.due_at.isoformat()}

    if payload.name is not None:
        c.name = payload.name.strip()
    if payload.description is not None:
        c.description = payload.description or None
    if payload.scope is not None:
        c.scope = payload.scope.model_dump(exclude_none=True)
    if payload.due_at is not None:
        if payload.due_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="due_at must be in the future",
            )
        c.due_at = payload.due_at

    new = {"name": c.name, "scope": c.scope, "due_at": c.due_at.isoformat()}
    await aaudit(
        db, "certification_campaign", c.id, "updated",
        old=old, new=new,
        by=actor_by(request, "update_certification_campaign"),
    )
    await db.commit()
    await db.refresh(c)
    return _campaign_dict(c, await _review_counts(db, c.id))


@router.delete(
    "/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[_WRITE_GATE, _WRITE_SCOPE],
)
async def delete_campaign(
    request: Request,
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    c = await db.get(CertificationCampaign, campaign_id)
    if not c:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found",
        )
    if c.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a running campaign — cancel it first",
        )
    await aaudit(
        db, "certification_campaign", c.id, "deleted",
        old={"name": c.name, "status": c.status},
        by=actor_by(request, "delete_certification_campaign"),
    )
    await db.delete(c)
    await db.commit()


# ── Kickoff + lifecycle transitions ────────────────────────────────────────────

async def _resolve_reviewer(
    db: AsyncSession, order: Order
) -> tuple[str, str | None]:
    """Pick the reviewer for ``order``.

    Priority:
    1. The first ``manager`` approver row on the order — captured at
       order-creation time, so this is the manager who originally
       approved access.
    2. The order's ``owner_email`` (deputy-ordering case — the deputy's
       manager isn't necessarily the right reviewer for the asset).
    3. The order's ``user_email`` (user reviews their own access — a
       degenerate fallback when no manager is on file).
    """
    rows = await db.execute(
        select(OrderApproval)
        .where(
            OrderApproval.order_id == order.id,
            OrderApproval.approver_type == "manager",
        )
        .order_by(OrderApproval.id)
        .limit(1)
    )
    mgr = rows.scalar_one_or_none()
    if mgr and mgr.approver_email:
        return mgr.approver_email, mgr.approver_name
    if order.owner_email:
        return order.owner_email, order.owner_name
    return order.user_email, order.user_name


def _matches_scope(scope: dict[str, Any], order: Order, asset_type: AssetType | None) -> bool:
    """AND across keys, OR within. Empty / missing keys are wildcards."""
    asset_type_ids = scope.get("asset_type_ids") or []
    if asset_type_ids and order.asset_type_id not in asset_type_ids:
        return False
    cost_centers = scope.get("cost_centers") or []
    if cost_centers:
        cc = (asset_type.cost_center if asset_type else None) or "(unassigned)"
        if cc not in cost_centers:
            return False
    departments = scope.get("departments") or []
    if departments:
        dept = order.requester_department or ""
        if dept not in departments:
            return False
    requester_emails = scope.get("requester_emails") or []
    if requester_emails:
        emails_lc = {e.strip().lower() for e in requester_emails if e.strip()}
        if (order.user_email or "").lower() not in emails_lc:
            return False
    return True


@router.post(
    "/{campaign_id}/start",
    dependencies=[_WRITE_GATE, _WRITE_SCOPE],
)
async def start_campaign(
    request: Request,
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Kickoff: scope → active orders → review rows. Idempotent only on
    failure (re-runs after a successful start are blocked by the
    status gate)."""
    c = await db.get(CertificationCampaign, campaign_id)
    if not c:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found",
        )
    if c.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot start a {c.status} campaign",
        )

    # Pull all active orders in one query, then filter in Python — the
    # scope-filter shape doesn't translate cleanly to SQL when half the
    # keys are wildcards. Reasonable for small / medium tenants
    # (typical campaigns scope to a department or asset type, not the
    # whole estate).
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.steps))
        .where(Order.status.in_([OrderStatus(s) for s in _ACTIVE_ORDER_STATUSES]))
    )
    orders = list(result.scalars().all())

    # Cache asset_types for cost-center lookup.
    asset_type_ids = {o.asset_type_id for o in orders if o.asset_type_id}
    asset_types: dict[int, AssetType] = {}
    if asset_type_ids:
        at_rows = await db.execute(
            select(AssetType).where(AssetType.id.in_(asset_type_ids))
        )
        asset_types = {at.id: at for at in at_rows.scalars().all()}

    scope = c.scope or {}
    created = 0
    for order in orders:
        at = asset_types.get(order.asset_type_id) if order.asset_type_id else None
        if not _matches_scope(scope, order, at):
            continue
        reviewer_email, reviewer_name = await _resolve_reviewer(db, order)
        if not reviewer_email:
            # Defensive — every active order has a user_email at minimum.
            continue
        review = CertificationReview(
            campaign_id=c.id,
            order_id=order.id,
            reviewer_email=reviewer_email.lower(),
            reviewer_name=reviewer_name,
            status="pending",
        )
        db.add(review)
        created += 1

    now = datetime.now(timezone.utc)
    c.status = "running"
    c.started_at = now

    actor = actor_by(request, "start_certification_campaign")
    await aaudit(
        db, "certification_campaign", c.id, "started",
        new={"started_at": now.isoformat(), "reviews_created": created},
        by=actor,
    )
    await db.commit()
    await db.refresh(c)

    # Best-effort kickoff notifications. Failures are logged but don't
    # roll back the campaign — admins can re-trigger via the daily Beat
    # task or by closing-and-restarting the campaign.
    notify_summary = await _dispatch_kickoff_notifications(db, c)

    logger.info(
        "certification: campaign %s started by %s — %d reviews created, %d kickoff emails",
        c.id, actor, created, notify_summary["emailed"],
    )
    return {
        **_campaign_dict(c, await _review_counts(db, c.id)),
        "reviews_created": created,
        "kickoff_emailed": notify_summary["emailed"],
        "kickoff_teams_sent": notify_summary["teams_sent"],
    }


async def _portal_base_url(db: AsyncSession) -> str:
    """Read ``portal.base_url`` for embedding into review-link emails."""
    result = await db.execute(
        select(AppConfig).where(AppConfig.key == "portal.base_url")
    )
    cfg = result.scalar_one_or_none()
    return (cfg.value if cfg and cfg.value else "http://localhost:8000").rstrip("/")


async def _dispatch_kickoff_notifications(
    db: AsyncSession, campaign: CertificationCampaign
) -> dict[str, int]:
    """Email + (optional) Teams card to each unique reviewer at kickoff.

    Per-reviewer aggregation: one email/card listing the count, link
    points at ``/review-queue/{token}`` so the reviewer sees all their
    rows in one place rather than receiving N separate emails.
    """
    rows = await db.execute(
        select(CertificationReview)
        .where(CertificationReview.campaign_id == campaign.id)
    )
    reviews = list(rows.scalars().all())
    if not reviews:
        return {"emailed": 0, "teams_sent": 0}

    by_reviewer: dict[str, list[CertificationReview]] = {}
    for r in reviews:
        by_reviewer.setdefault(r.reviewer_email, []).append(r)

    base = await _portal_base_url(db)
    teams_cfg = await db.execute(
        select(AppConfig).where(AppConfig.key.in_(("teams.mode", "teams.webhook_url", "app.title")))
    )
    cfg_map = {c.key: (c.value or "") for c in teams_cfg.scalars().all()}
    # Resolve the webhook URL once here (it's is_secret=true and may be
    # a vault://… / conjur://… reference) so the worker tasks receive a
    # ready-to-POST URL — keeps notification workers free of resolver
    # plumbing.
    from app.utils.secrets import resolve_secret_value
    teams_webhook_resolved = (await resolve_secret_value(db, cfg_map.get("teams.webhook_url", ""))).strip()
    teams_enabled = (cfg_map.get("teams.mode", "disabled").strip() == "enabled"
                     and bool(teams_webhook_resolved))
    app_title = cfg_map.get("app.title", "ip·Solis") or "ip·Solis"

    due_date = campaign.due_at.strftime("%Y-%m-%d") if campaign.due_at else ""

    # Dispatch emails + Teams cards via Celery so the start request
    # returns immediately. The worker reads back the campaign + review
    # rows and renders the actual notifications.
    from celery import Celery
    from app.config import settings as _settings
    celery_app = Celery(broker=_settings.CELERY_BROKER_URL)

    emailed = 0
    teams_sent = 0
    for reviewer_email, rs in by_reviewer.items():
        # Per-reviewer queue link uses the first row's token (the queue
        # endpoint expands it to the reviewer's full pending list).
        first = rs[0]
        queue_token = make_review_token(first.id)
        review_url = f"{base}/review-queue/{queue_token}"

        celery_app.send_task(
            "tasks.workflows.certification_notifications.send_kickoff_email",
            args=[
                reviewer_email,
                first.reviewer_name or reviewer_email,
                campaign.name,
                campaign.id,
                len(rs),
                due_date,
                review_url,
                teams_enabled,
                teams_webhook_resolved,
                app_title,
            ],
            queue="notifications",
        )
        emailed += 1
        if teams_enabled:
            teams_sent += 1

    return {"emailed": emailed, "teams_sent": teams_sent}


@router.post(
    "/{campaign_id}/close",
    dependencies=[_WRITE_GATE, _WRITE_SCOPE],
)
async def close_campaign(
    request: Request,
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Close a running campaign. Pending reviews stay pending — they
    won't auto-revoke once the campaign is closed (slice 2 will gate
    that on campaign status). Use this when an audit cycle wraps up
    and the remaining unreviewed orders are deemed acceptable."""
    c = await db.get(CertificationCampaign, campaign_id)
    if not c:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found",
        )
    if c.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot close a {c.status} campaign",
        )
    c.status = "closed"
    c.closed_at = datetime.now(timezone.utc)
    counts = await _review_counts(db, c.id)
    await aaudit(
        db, "certification_campaign", c.id, "closed",
        new={"closed_at": c.closed_at.isoformat(), "review_counts": counts},
        by=actor_by(request, "close_certification_campaign"),
    )
    await db.commit()
    await db.refresh(c)
    return _campaign_dict(c, counts)


@router.post(
    "/{campaign_id}/cancel",
    dependencies=[_WRITE_GATE, _WRITE_SCOPE],
)
async def cancel_campaign(
    request: Request,
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel a draft or running campaign. Pending reviews stay in the
    table for audit trail; their status remains pending but the
    parent campaign is terminal. Recorded distinctly from ``closed``
    so auditors can tell "we wrapped it up" from "we abandoned it"."""
    c = await db.get(CertificationCampaign, campaign_id)
    if not c:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign {campaign_id} not found",
        )
    if c.status in ("closed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Campaign is already {c.status}",
        )
    c.status = "cancelled"
    c.closed_at = datetime.now(timezone.utc)
    await aaudit(
        db, "certification_campaign", c.id, "cancelled",
        new={"closed_at": c.closed_at.isoformat()},
        by=actor_by(request, "cancel_certification_campaign"),
    )
    await db.commit()
    await db.refresh(c)
    return _campaign_dict(c, await _review_counts(db, c.id))


# ── Review decisions (admin-side; portal flow lands in slice 2) ─────────────

@router.post(
    "/{campaign_id}/reviews/{review_id}/decide",
    dependencies=[_WRITE_GATE, _WRITE_SCOPE],
)
async def decide_review(
    request: Request,
    campaign_id: int,
    review_id: int,
    payload: ReviewDecision,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Admin records a confirm/revoke decision on behalf of the reviewer.

    Slice 2 will add a portal page where the reviewer themselves can
    decide via a signed-token link (no admin session required). For
    slice 1, an admin can stand in for the reviewer to validate the
    flow end-to-end.

    On ``revoked``: marks the underlying order as ``revoking`` and
    dispatches the asset's deprovision runbook so access is actually
    pulled. On ``confirmed``: review row only — no order side-effects.
    """
    review = await db.get(CertificationReview, review_id)
    if not review or review.campaign_id != campaign_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review {review_id} not found in campaign {campaign_id}",
        )
    if review.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Review is already {review.status}",
        )
    campaign = await db.get(CertificationCampaign, campaign_id)
    if campaign and campaign.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot decide reviews on a {campaign.status} campaign",
        )

    now = datetime.now(timezone.utc)
    actor = actor_by(request, "decide_certification_review")
    review.status = payload.decision
    review.decided_at = now
    review.decided_by = actor
    review.comment = (payload.comment or "").strip() or None

    await aaudit(
        db, "certification_review", review.id, payload.decision,
        new={
            "campaign_id": review.campaign_id,
            "order_id": review.order_id,
            "reviewer_email": review.reviewer_email,
            "comment": review.comment,
        },
        by=actor,
    )

    if payload.decision == "revoked":
        order = await db.get(Order, review.order_id)
        if order and order.status not in (
            OrderStatus.REVOKED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
        ):
            old_status = order.status.value
            order.status = OrderStatus.REVOKING
            from app.routes.webhook import _dispatch_runbook
            from app.models.order import OrderAction
            order.action = OrderAction.DELETE
            _dispatch_runbook(order)
            await aaudit(
                db, "order", order.id, "status_changed",
                old={"status": old_status},
                new={
                    "status": OrderStatus.REVOKING.value,
                    "reason": f"Revoked by certification review #{review.id}",
                },
                by=actor,
            )

    await db.commit()
    await db.refresh(review)
    return _review_dict(review)
