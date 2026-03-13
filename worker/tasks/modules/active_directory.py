"""Module: Active Directory – LDAP user lookup via msldap.

Reads AD configuration (server, base DN, credentials) from the app_config table.
Returns user information (name, email, department).

Uses msldap instead of ldap3 because msldap supports NTLM with message signing,
which is required by modern Windows Server AD (LDAPServerIntegrity = Require signing).

Corresponds to the Ivanti module 'QAD Lookup' (standard LDAP instead of Quest AD).
"""

import logging

from sqlalchemy.orm import Session

from tasks.modules.config_reader import get_config, get_config_int

logger = logging.getLogger(__name__)


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
    return _ldap_lookup_user(identifier, db)


def _ldap_lookup_user(identifier: str, db: Session) -> dict:
    """Real LDAP lookup via msldap (supports NTLM signing)."""
    import asyncio

    server_host = get_config(db, "ad.server", "dc.example.com")
    server_port = get_config_int(db, "ad.port", 389)
    base_dn = get_config(db, "ad.base_dn", "DC=example,DC=com")
    bind_user = get_config(db, "ad.username", "")
    bind_password = get_config(db, "ad.password", "")
    domain = get_config(db, "ad.domain", "")

    # sAMAccountName or mail filter depending on identifier type
    if "@" in identifier:
        ldap_filter = f"(mail={identifier})"
    else:
        sam = identifier.split("\\")[-1] if "\\" in identifier else identifier
        ldap_filter = f"(sAMAccountName={sam})"

    try:
        result = asyncio.run(_msldap_lookup(
            server_host, server_port, domain, bind_user, bind_password,
            base_dn, ldap_filter, identifier
        ))
        return result
    except Exception as e:
        logger.error("LDAP lookup failed for '%s': %s", identifier, e)
        return {"success": False, "error": str(e)}


async def _msldap_lookup(server_host, server_port, domain, bind_user, bind_password,
                          base_dn, ldap_filter, identifier) -> dict:
    from msldap.commons.factory import LDAPConnectionFactory
    from urllib.parse import quote

    user_escaped = quote(f"{domain}\\{bind_user}" if domain else bind_user, safe="")
    pass_escaped = quote(bind_password, safe="")
    url = f"ldap+ntlm-password://{user_escaped}:{pass_escaped}@{server_host}:{server_port}"

    factory = LDAPConnectionFactory.from_url(url)
    client = factory.get_client()
    await client.connect()

    try:
        attrs = ["mail", "displayName", "givenName", "sn", "department", "sAMAccountName",
                 "distinguishedName"]
        found = None
        async for entry, err in client.pagedsearch(ldap_filter, attrs, tree=base_dn):
            if err:
                raise Exception(str(err))
            found = entry["attributes"]
            break

        if not found:
            return {"success": False, "error": f"User '{identifier}' not found in AD"}

        def _attr(key):
            v = found.get(key)
            if isinstance(v, list):
                return v[0] if v else None
            return str(v) if v else None

        return {
            "success": True,
            "email": _attr("mail") or identifier,
            "display_name": _attr("displayName") or identifier,
            "first_name": _attr("givenName"),
            "last_name": _attr("sn"),
            "department": _attr("department"),
            "sam_account": _attr("sAMAccountName"),
            "dn": _attr("distinguishedName"),
        }
    finally:
        await client.disconnect()

