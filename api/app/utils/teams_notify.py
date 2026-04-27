"""Microsoft Teams notification sender (Workflows webhook).

Pairs with a Teams Workflow whose trigger is "When a webhook request is
received" and action is "Post Adaptive Card in chat or channel". The
admin pastes the workflow URL into Settings → Notifications → Teams.

We deliberately use stdlib ``urllib`` rather than httpx so the worker
container (which doesn't import the API package) can mirror this module
verbatim with zero dependency drift.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10


def post_adaptive_card(webhook_url: str, card: dict[str, Any]) -> tuple[bool, str]:
    """POST an Adaptive Card to a Teams Workflow webhook URL.

    Returns ``(success, message)``. Never raises — failed delivery should
    not block the surrounding workflow.
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
    except Exception as e:  # noqa: BLE001 — defensive, any failure is a non-fatal config issue
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
    """Build an Adaptive Card payload for an approval request.

    Single "Review request" button — the linked page (signed-token endpoint)
    shows the order details and lets the approver pick Approve or Decline
    with an optional comment. We intentionally avoid one-click GET-based
    approval here because Outlook and link previewers prefetch URLs.

    When ``approver_email`` is set, the card includes a Teams
    ``msteams.entities`` block with an ``@mention`` so the approver gets a
    real banner/push notification (channel posts authored "by you via
    Workflows" don't notify the author by default — explicit @mentions do).
    """
    facts = [
        {"title": "Asset", "value": asset_type_name or "(unknown)"},
        {"title": "Requester", "value": f"{requester_name} <{requester_email}>"},
    ]
    if from_date:
        facts.append({"title": "From", "value": from_date})
    if until_date:
        facts.append({"title": "Until", "value": until_date})

    # See worker/tasks/modules/teams_notify.py for the rationale on using the
    # approver's name as the <at> placeholder rather than a synthetic token.
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
