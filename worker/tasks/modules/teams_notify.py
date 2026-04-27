"""Teams notification + approval-token utilities for the worker.

Mirrors ``api/app/utils/teams_notify.py`` and ``api/app/utils/approval_token.py``
so the worker can build approval cards and signed approval URLs without
importing the api package (separate Docker image, separate dep set).

Keep these in sync if either side changes — they're intentionally duplicated
because cross-image imports aren't supported. Token format / signing key are
identical so URLs minted on either side verify on the api endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10
_DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days

# ── Approval token (HMAC-SHA256, identical to api/app/utils/approval_token.py) ──

def _signing_key() -> bytes:
    return os.environ.get("API_SECRET_KEY", "change_me_in_production_min_32_chars").encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_approval_token(approval_id: int, ttl_seconds: int | None = None) -> str:
    payload: dict[str, Any] = {
        "aid": int(approval_id),
        "exp": int(time.time()) + int(ttl_seconds or _DEFAULT_TTL_SECONDS),
        "v": 1,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64url_encode(raw)
    sig = hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


# ── Teams Workflow webhook sender ──────────────────────────────────────────────

def post_adaptive_card(webhook_url: str, card: dict[str, Any]) -> tuple[bool, str]:
    """POST an Adaptive Card to a Teams Workflow webhook URL.

    Returns ``(success, message)``. Never raises.
    """
    if not webhook_url or not webhook_url.strip():
        return False, "Teams webhook URL is not configured."

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url.strip(),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            status = resp.status
            if 200 <= status < 300:
                return True, f"Posted to Teams (HTTP {status})."
            return False, f"Teams responded with HTTP {status}."
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"Network error: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def build_approval_card(
    *,
    asset_type_name: str,
    requester_name: str,
    requester_email: str,
    approver_name: str,
    review_url: str,
    approver_email: str = "",
    from_date: str = "",
    until_date: str = "",
    app_title: str = "ip·Solis",
) -> dict[str, Any]:
    """Build an Adaptive Card for an approval request.

    When ``approver_email`` is set, the card includes a Teams ``msteams.entities``
    block with an ``@mention`` of the approver. Teams generates a real
    notification (banner / mobile push) for an explicit @mention even when
    the post is authored by "the user, via workflows" — without it, channel
    posts authored by the actor themselves yield no notification on their
    own client. The placeholder token ``approver`` is intentionally
    synthetic: matching between body ``<at>approver</at>`` and the entity
    ``text`` is byte-exact, so a synthetic token avoids any escaping
    issues with names containing ``<``, ``>``, or ``&``.
    """
    facts = [
        {"title": "Asset", "value": asset_type_name or "(unknown)"},
        {"title": "Requester", "value": f"{requester_name} <{requester_email}>"},
    ]
    if from_date:
        facts.append({"title": "From", "value": from_date})
    if until_date:
        facts.append({"title": "Until", "value": until_date})

    # Use the approver's display name as the <at> placeholder. When the
    # Workflow template forwards msteams.entities, Teams renders this as a
    # real @mention with notification. When it doesn't (most "Post to
    # channel via webhook" templates strip entities), Teams still displays
    # the inner text — so the user sees their actual name, not a generic
    # placeholder. ``<>&`` are stripped from the inner text because the
    # body/entity match is byte-exact and these chars confuse some renderers.
    safe_name = "".join(c for c in (approver_name or "") if c not in "<>&").strip()
    msteams: dict[str, Any] = {"width": "Full"}
    if approver_email and approver_email.strip() and safe_name:
        greeting = f"Hi <at>{safe_name}</at>,"
        msteams["entities"] = [{
            "type": "mention",
            "text": f"<at>{safe_name}</at>",
            "mentioned": {
                "id": approver_email.strip(),
                "name": safe_name,
            },
        }]
    else:
        greeting = f"Hi {approver_name}," if approver_name else "Hi,"

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": msteams,
        "body": [
            {
                "type": "TextBlock",
                "text": f"{app_title} — Access request awaiting approval",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": greeting,
                "wrap": True,
                "spacing": "Small",
            },
            {
                "type": "FactSet",
                "facts": facts,
            },
            {
                "type": "TextBlock",
                "text": "Click below to review and approve or decline.",
                "wrap": True,
                "size": "Small",
                "isSubtle": True,
                "spacing": "Medium",
            },
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Review request →",
                "url": review_url,
                "style": "positive",
            }
        ],
    }
