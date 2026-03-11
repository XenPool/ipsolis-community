"""AD lookup helper for the API.

Lightweight version of the worker module active_directory.py.
Uses the same mock (all identifiers accepted) in dev mode.
In production: ldap3 (must be added to requirements.txt).
"""
import logging
import os

logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

_MOCK_USERS: dict[str, dict] = {
    "s.muster": {
        "success": True,
        "email": "stefan.muster@xenpool.de",
        "display_name": "Stefan Muster",
        "sam_account": "s.muster",
    },
    "p.nutzer": {
        "success": True,
        "email": "peter.nutzer@xenpool.de",
        "display_name": "Peter Nutzer",
        "sam_account": "p.nutzer",
    },
}


def lookup_user(identifier: str) -> dict:
    """
    Looks up a user in Active Directory.

    Args:
        identifier: sAMAccountName (z.B. "s.muster") oder E-Mail

    Returns:
        {"success": True, "display_name": str, "email": str, "sam_account": str}
        {"success": False, "error": str}
    """
    if not identifier.strip():
        return {"success": False, "error": "Leere Eingabe"}

    if ENVIRONMENT == "development":
        return _mock_lookup(identifier)

    return _ldap_lookup(identifier)


def _mock_lookup(identifier: str) -> dict:
    sam = identifier.split("\\")[-1] if "\\" in identifier else identifier
    sam = sam.split("@")[0].lower().strip()

    if sam in _MOCK_USERS:
        result = _MOCK_USERS[sam].copy()
        logger.info("[MOCK] AD found: %s (%s)", result["display_name"], result["email"])
        return result

    # Generic fallback: accept identifier as valid user
    display = identifier.replace("\\", " ").replace(".", " ").replace("@", " ").title().strip()
    email = identifier if "@" in identifier else f"{sam}@xenpool.de"
    logger.info("[MOCK] AD fallback for '%s'", identifier)
    return {
        "success": True,
        "email": email,
        "display_name": display or identifier,
        "sam_account": sam,
    }


def _ldap_lookup(identifier: str) -> dict:
    try:
        import ldap3
        import ldap3.utils.conv
    except ImportError:
        return {"success": False, "error": "ldap3 not installed (add to requirements.txt)"}

    # Config from environment variables (fallback to defaults)
    server_host = os.getenv("AD_SERVER", "dc.example.com")
    server_port = int(os.getenv("AD_PORT", "389"))
    base_dn = os.getenv("AD_BASE_DN", "DC=example,DC=com")
    bind_user = os.getenv("AD_USERNAME", "")
    bind_password = os.getenv("AD_PASSWORD", "")
    domain = os.getenv("AD_DOMAIN", "")

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
            attributes=["mail", "displayName", "sAMAccountName"],
        )
        if not conn.entries:
            return {"success": False, "error": f"Benutzer '{identifier}' nicht im AD gefunden"}

        entry = conn.entries[0]
        return {
            "success": True,
            "email": str(entry.mail) if entry.mail else identifier,
            "display_name": str(entry.displayName) if entry.displayName else identifier,
            "sam_account": str(entry.sAMAccountName) if entry.sAMAccountName else identifier,
        }
    except Exception as e:
        logger.error("LDAP lookup failed for '%s': %s", identifier, e)
        return {"success": False, "error": str(e)}
