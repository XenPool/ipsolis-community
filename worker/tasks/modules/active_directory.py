"""Module: Active Directory – LDAP user lookup via ldap3.

Reads AD configuration (server, base DN, credentials) from the app_config table.
Returns user information (name, email, department).

Corresponds to the Ivanti module 'QAD Lookup' (standard LDAP instead of Quest AD).
"""

import logging
import os

from sqlalchemy.orm import Session

from tasks.modules.config_reader import get_config, get_config_int

logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")


def lookup_user(identifier: str, db: Session) -> dict:
    """
    Looks up a user in Active Directory.

    Args:
        identifier: Email address or sAMAccountName (e.g. "s.muster" or "s.muster@example.com")
        db:         Sync SQLAlchemy Session (for app_config read access)

    Returns:
        Success: {"success": True,  "email": str, "display_name": str,
                  "first_name": str, "last_name": str, "department": str | None}
        Error:   {"success": False, "error": str}
    """
    if ENVIRONMENT == "development":
        return _mock_lookup_user(identifier)

    return _ldap_lookup_user(identifier, db)


def _ldap_lookup_user(identifier: str, db: Session) -> dict:
    """Real LDAP lookup via ldap3."""
    try:
        import ldap3
    except ImportError:
        return {"success": False, "error": "ldap3 not installed"}

    server_host = get_config(db, "ad.server", "dc.example.com")
    server_port = get_config_int(db, "ad.port", 389)
    base_dn = get_config(db, "ad.base_dn", "DC=example,DC=com")
    bind_user = get_config(db, "ad.username", "")
    bind_password = get_config(db, "ad.password", "")
    domain = get_config(db, "ad.domain", "")

    # sAMAccountName or mail filter depending on identifier type
    if "@" in identifier:
        ldap_filter = f"(mail={ldap3.utils.conv.escape_filter_chars(identifier)})"
    else:
        sam = identifier.split("\\")[-1] if "\\" in identifier else identifier
        ldap_filter = f"(sAMAccountName={ldap3.utils.conv.escape_filter_chars(sam)})"

    bind_dn = f"{domain}\\{bind_user}" if domain else bind_user

    try:
        server = ldap3.Server(server_host, port=server_port, get_info=ldap3.NONE)
        conn = ldap3.Connection(server, user=bind_dn, password=bind_password, auto_bind=True)

        conn.search(
            search_base=base_dn,
            search_filter=ldap_filter,
            search_scope=ldap3.SUBTREE,
            attributes=["mail", "displayName", "givenName", "sn", "department", "sAMAccountName"],
        )

        if not conn.entries:
            return {"success": False, "error": f"User '{identifier}' not found in AD"}

        entry = conn.entries[0]

        def _attr(name: str) -> str | None:
            val = getattr(entry, name, None)
            return str(val) if val else None

        return {
            "success": True,
            "email": _attr("mail") or identifier,
            "display_name": _attr("displayName") or identifier,
            "first_name": _attr("givenName"),
            "last_name": _attr("sn"),
            "department": _attr("department"),
            "sam_account": _attr("sAMAccountName"),
        }

    except Exception as e:
        logger.error("LDAP lookup failed for '%s': %s", identifier, e)
        return {"success": False, "error": str(e)}


# ── Mocks ─────────────────────────────────────────────────────────────────────

_MOCK_USERS: dict[str, dict] = {
    "s.muster": {
        "success": True,
        "email": "stefan.muster@xenpool.de",
        "display_name": "Stefan Muster",
        "first_name": "Stefan",
        "last_name": "Muster",
        "department": "IT",
        "sam_account": "s.muster",
    },
    "p.nutzer": {
        "success": True,
        "email": "peter.nutzer@xenpool.de",
        "display_name": "Peter Nutzer",
        "first_name": "Peter",
        "last_name": "Nutzer",
        "department": "Finance",
        "sam_account": "p.nutzer",
    },
}


def _mock_lookup_user(identifier: str) -> dict:
    logger.info("[MOCK] AD lookup for '%s'", identifier)

    # Normalize: remove domain prefix and @ for mock lookup
    sam = identifier.split("\\")[-1] if "\\" in identifier else identifier
    sam = sam.split("@")[0].lower()

    if sam in _MOCK_USERS:
        result = _MOCK_USERS[sam].copy()
        logger.info("[MOCK] AD found: %s (%s)", result["display_name"], result["email"])
        return result

    # Generic fallback: return identifier as mock user
    display = identifier.replace("\\", " ").replace(".", " ").replace("@", " ").title()
    logger.info("[MOCK] AD not found, generating fallback for '%s'", identifier)
    return {
        "success": True,
        "email": identifier if "@" in identifier else f"{sam}@xenpool.de",
        "display_name": display,
        "first_name": display.split()[0] if display.split() else display,
        "last_name": display.split()[-1] if len(display.split()) > 1 else "",
        "department": None,
        "sam_account": sam,
    }
