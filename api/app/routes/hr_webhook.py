"""HR leaver webhook receiver.

Generic JSON endpoint for HR systems (Workday, SAP SuccessFactors,
Microsoft Graph leaver events, BambooHR, …) to push a "this user has
left the organisation" signal. ip·Solis triggers the unified leaver
flow which revokes every active order owned by the user, supersedes
their pending approvals, and supersedes any pending certification
reviews assigned to them.

Authentication mirrors the ServiceNow webhook — either of:

1. ``Authorization: Bearer xpat_…`` with the ``hr:leaver`` scope.
   Preferred for new integrations: revocable from the Admin UI without
   touching the running container.
2. ``X-Hub-Signature-256: sha256=…`` HMAC over the raw body using
   ``WEBHOOK_SECRET_TOKEN``. Kept as a fallback so HR systems that
   sign with a shared secret (Workday integration system, SAP IDM)
   work without minting a per-integration token.

Payload shape: a normalized JSON object that vendor adapters
translate into. The minimal contract is one of:

```json
{ "event": "leaver", "email": "alice@example.com" }

{ "event": "leaver",
  "email": "alice@example.com",
  "external_id": "EMP-12345",
  "source": "workday" }
```

Vendor-specific shapes (Workday's ``$.workerId`` + ``$.email``, SAP's
``$.PERSON.PERNR``) are translated by the ``_normalise`` function
below — keep adding cases as new vendors come online rather than
spreading vendor-specific quirks across the rest of the app.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.hr_leaver_event import HrLeaverEvent
from app.utils.auth import require_admin_key, require_scopes
from app.utils.features import require_enterprise
from app.utils.leaver import process_leaver
from app.utils.rbac import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/hr",
    tags=["hr-webhook"],
    dependencies=[require_enterprise("hr_webhook")],
)


def _verify_hmac(body: bytes, signature: str) -> bool:
    expected = hmac.new(
        settings.WEBHOOK_SECRET_TOKEN.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


async def _authenticate(
    request: Request,
    db: AsyncSession,
    x_hub_signature_256: str | None,
) -> str:
    """Auth by bearer token (preferred) or HMAC (back-compat).

    Returns an actor string suitable for the audit ``triggered_by``
    column. Raises ``HTTPException`` on any auth failure.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        raw = auth_header.split(" ", 1)[1].strip()
        from app.utils.api_tokens import mark_used, token_has_scope, verify_raw_token

        token = await verify_raw_token(db, raw)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        scopes = list(token.scopes or [])
        if not token_has_scope(scopes, "hr:leaver"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Token '{token.name}' lacks required scope 'hr:leaver'. "
                    f"Granted: {', '.join(sorted(scopes)) or '(none)'}."
                ),
            )
        await mark_used(db, token.id)
        await db.commit()
        return f"hr:token:{token.name}"

    if not x_hub_signature_256:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "HR webhook authentication required. Send either "
                "'Authorization: Bearer <xpat_…>' (with scope hr:leaver) "
                "or 'X-Hub-Signature-256: sha256=<HMAC>'."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    body = await request.body()
    if not _verify_hmac(body, x_hub_signature_256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )
    return "hr:hmac"


def _normalise(payload: dict[str, Any]) -> tuple[str, str | None, str]:
    """Vendor-shape adapter → ``(email, external_id, source)``.

    Recognises:
    * ipSolis-native: ``{event, email, external_id?, source?}``
    * Workday: ``{workerId, primaryEmail | workEmail | email, eventType=='terminated'}``
    * SAP SuccessFactors: ``{PERSON: {PERNR, email}}``
    * Microsoft Graph subscription: ``{value: [{resourceData: {userPrincipalName}}]}``
      (treats the Graph "deleted" event as a leaver signal — adapter
      keeps the source label so audit attribution is honest)

    Raises ``HTTPException(400)`` on a payload it can't recognise. New
    vendors should land here as a new ``elif`` rather than spreading
    vendor quirks elsewhere.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    # ipSolis-native shape
    if isinstance(payload.get("email"), str):
        return (
            payload["email"].strip(),
            (payload.get("external_id") or payload.get("externalId") or None),
            (payload.get("source") or "hr_webhook").strip(),
        )

    # Workday — `workerId` + an email field, eventType filter.
    if "workerId" in payload:
        event_type = (payload.get("eventType") or "").lower()
        if event_type and event_type not in ("terminated", "termination", "leaver"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Workday eventType {event_type!r} is not a leaver event",
            )
        email = (
            payload.get("primaryEmail")
            or payload.get("workEmail")
            or payload.get("email")
            or ""
        )
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workday payload missing primaryEmail / workEmail / email",
            )
        return email.strip(), str(payload["workerId"]), "workday"

    # SAP SuccessFactors — PERSON envelope.
    if isinstance(payload.get("PERSON"), dict):
        person = payload["PERSON"]
        email = person.get("email") or person.get("EMAIL") or ""
        pernr = person.get("PERNR") or person.get("pernr")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SAP payload missing PERSON.email",
            )
        return email.strip(), str(pernr) if pernr else None, "sap"

    # Microsoft Graph subscription notification (user.deleted change type).
    if isinstance(payload.get("value"), list) and payload["value"]:
        first = payload["value"][0]
        if isinstance(first, dict) and isinstance(first.get("resourceData"), dict):
            rd = first["resourceData"]
            upn = rd.get("userPrincipalName") or rd.get("mail")
            obj_id = rd.get("id")
            if upn:
                return upn.strip(), str(obj_id) if obj_id else None, "msgraph"

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "Could not normalise HR leaver payload. Provide {email} at minimum, "
            "or one of the supported vendor shapes (workday / sap / msgraph)."
        ),
    )


@router.post("/leaver", status_code=status.HTTP_200_OK)
async def hr_leaver(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    """Receive a leaver event, normalise the payload, run the leaver flow."""
    actor = await _authenticate(request, db, x_hub_signature_256)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be valid JSON")

    email, external_id, source = _normalise(payload)

    summary = await process_leaver(
        db,
        user_email=email,
        source=source,
        triggered_by=f"api:hr_leaver ({actor})",
        user_external_id=external_id,
        raw_payload=payload,
    )
    logger.info(
        "hr leaver processed: email=%s source=%s actor=%s revoked=%d "
        "approvals_superseded=%d reviews_superseded=%d",
        email, source, actor,
        summary["orders_revoked"],
        summary["approvals_superseded"],
        summary["reviews_superseded"],
    )
    return summary


# ── Admin-side read API for the leaver events audit table ────────────────────
# Lives on a separate sub-router so the audit-read floor is auditor+ while
# the webhook-write path stays HMAC / scoped-token gated above. Mounting on
# the same /hr prefix keeps the URL surface coherent.

_admin_router = APIRouter(
    prefix="/hr/admin",
    tags=["hr-admin"],
    # Enterprise-gated: the leaver-events viewer is the read-side of
    # the HR webhook + SCIM provisioning features (both already gated).
    # On a community install the table is always empty anyway since no
    # webhook/SCIM events can land — gating prevents confusion.
    dependencies=[
        Depends(require_admin_key),
        require_enterprise("hr_leaver_events"),
        require_scopes("audit:read"),
        require_role("auditor"),
    ],
)


@_admin_router.get("/leaver-events")
async def list_leaver_events(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user_email: str | None = Query(default=None),
) -> list[dict]:
    """Return recent leaver events, newest first.

    ``user_email`` is a case-insensitive substring filter. ``limit`` /
    ``offset`` for cursor-style paging.
    """
    query = select(HrLeaverEvent).order_by(HrLeaverEvent.received_at.desc())
    if user_email:
        query = query.where(HrLeaverEvent.user_email.ilike(f"%{user_email.strip()}%"))
    query = query.offset(offset).limit(limit)
    rows = await db.execute(query)
    return [
        {
            "id": e.id,
            "source": e.source,
            "user_email": e.user_email,
            "user_external_id": e.user_external_id,
            "status": e.status,
            "error_message": e.error_message,
            "orders_revoked": e.orders_revoked,
            "approvals_superseded": e.approvals_superseded,
            "reviews_superseded": e.reviews_superseded,
            "received_at": e.received_at.isoformat() if e.received_at else None,
            "processed_at": e.processed_at.isoformat() if e.processed_at else None,
            "triggered_by": e.triggered_by,
        }
        for e in rows.scalars().all()
    ]


# Re-export so main.py can register the admin sub-router alongside.
admin_router = _admin_router
