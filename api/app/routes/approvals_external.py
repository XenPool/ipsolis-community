"""Tokenized approval endpoint — no portal session required.

The approver clicks a link from their email or Teams card. The token in the
URL identifies which OrderApproval row they're acting on; we trust the token
because it's HMAC-signed with our internal API_SECRET_KEY (see
``app.utils.approval_token``). The link works from any client (Outlook,
Teams, mobile mail) without forcing the user through Entra SSO first.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.approval import OrderApproval
from app.models.asset import AssetType
from app.models.order import Order
from app.templates_instance import templates
from app.utils.approval_decision import SoDViolation, apply_approval_decision
from app.utils.approval_token import verify_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["approvals-external"])


class _LookupResult:
    """Outcome of resolving a tokenised approval link."""
    __slots__ = ("approval", "reason")

    def __init__(self, approval: OrderApproval | None, reason: str) -> None:
        self.approval = approval
        self.reason = reason  # "ok" | "bad_token" | "missing_row"


async def _load_approval_by_token(db: AsyncSession, token: str) -> _LookupResult:
    payload = verify_token(token)
    if payload is None:
        return _LookupResult(None, "bad_token")
    result = await db.execute(
        select(OrderApproval).where(OrderApproval.id == payload["aid"])
    )
    approval = result.scalar_one_or_none()
    if approval is None:
        # Token signature is fine and not expired, but the approval row is
        # gone — typically because the owning order was deleted (cascade)
        # or the row was administratively cleaned up.
        return _LookupResult(None, "missing_row")
    return _LookupResult(approval, "ok")


def _render_status_page(
    request: Request,
    *,
    title: str,
    headline: str,
    message: str,
    tone: str = "info",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "approve_status.html",
        {
            "request": request,
            "title": title,
            "headline": headline,
            "message": message,
            "tone": tone,  # "info" | "success" | "warning" | "error"
        },
        status_code=status_code,
    )


@router.get("/approve/{token}", response_class=HTMLResponse)
async def approve_get(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Render the approve / decline confirmation page for a tokenized link."""
    lookup = await _load_approval_by_token(db, token)
    if lookup.reason == "bad_token":
        return _render_status_page(
            request,
            title="Link invalid",
            headline="This approval link is invalid or has expired.",
            message=(
                "The link may have expired (links are valid for 14 days) or the "
                "secret used to sign it has rotated. Open the portal directly to "
                "find pending approvals."
            ),
            tone="warning",
            status_code=410,
        )
    if lookup.reason == "missing_row":
        return _render_status_page(
            request,
            title="Request no longer exists",
            headline="This request is no longer in the system.",
            message=(
                "The associated order may have been cancelled or deleted before "
                "you reached this page. Open the portal to see your current pending approvals."
            ),
            tone="info",
            status_code=404,
        )

    approval = lookup.approval
    if approval.status != "pending":
        return _render_status_page(
            request,
            title="Already decided",
            headline=f"This request has already been {approval.status}.",
            message="No further action is needed.",
            tone="info",
        )

    # Hydrate context for the form
    order_result = await db.execute(
        select(Order).options(selectinload(Order.steps)).where(Order.id == approval.order_id)
    )
    order = order_result.scalar_one_or_none()
    asset_type = await db.get(AssetType, order.asset_type_id) if order else None

    return templates.TemplateResponse(
        "approve_confirm.html",
        {
            "request": request,
            "approval": approval,
            "order": order,
            "asset_type": asset_type,
            "token": token,
        },
    )


@router.post("/approve/{token}", response_class=HTMLResponse)
async def approve_post(
    request: Request,
    token: str,
    decision: str = Form(...),
    comment: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Record the decision encoded in the form against the token's approval row."""
    lookup = await _load_approval_by_token(db, token)
    if lookup.reason == "bad_token":
        raise HTTPException(status_code=410, detail="Approval link invalid or expired")
    if lookup.reason == "missing_row":
        raise HTTPException(status_code=404, detail="Approval row no longer exists")
    approval = lookup.approval

    if decision not in ("approve", "reject", "decline"):
        raise HTTPException(status_code=400, detail="Invalid decision")
    # Normalize: portal uses 'approve' | else; we accept reject/decline as decline.
    norm = "approve" if decision == "approve" else "reject"

    # Audit attribution names the signed-token path explicitly so a
    # decision made via an emailed link is distinguishable from a
    # portal-session decision in the audit log.
    actor = f"api:approval_token (approver:{approval.approver_email})"
    try:
        result = await apply_approval_decision(db, approval, norm, comment, actor=actor)
    except SoDViolation as exc:
        return _render_status_page(
            request,
            title="Cannot approve",
            headline="Separation of duties — you configured this asset type.",
            message=(
                f"You appear in the audit trail as a configurer of asset type "
                f"{exc.asset_type_id}. Ask a different approver to decide."
            ),
            tone="error",
        )

    if result.status == "already_decided":
        return _render_status_page(
            request,
            title="Already decided",
            headline=f"This request has already been {approval.status}.",
            message="No further action is needed.",
            tone="info",
        )

    if result.status == "approved":
        if result.all_granted:
            return _render_status_page(
                request,
                title="Approved",
                headline="Approval recorded — order is being dispatched.",
                message="The requester will receive a confirmation email.",
                tone="success",
            )
        return _render_status_page(
            request,
            title="Approved",
            headline="Your approval is recorded.",
            message="One or more additional approvers still need to review this request.",
            tone="success",
        )

    return _render_status_page(
        request,
        title="Declined",
        headline="Decision recorded.",
        message="The requester has been notified that the order was declined.",
        tone="info",
    )
