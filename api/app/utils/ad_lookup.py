"""AD lookup helper for the API.

Uses msldap to validate user identifiers (sAMAccountName or email) against
on-premises Active Directory.  msldap supports NTLM with message signing,
which modern Windows Server AD requires (LDAPServerIntegrity = Require signing).

AD config is read from env vars first; if AD_SERVER is not set, falls back
to the app_config table (configured via Admin -> Settings -> Active Directory).

When AD is not configured, lookup functions return an error indicating that
AD must be set up via the Admin UI.
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def _load_ad_config_from_db() -> dict:
    """Read AD config from app_config table via synchronous psycopg2 connection."""
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        return {}
    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        import psycopg2
        conn = psycopg2.connect(sync_url)
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM app_config WHERE key LIKE 'ad.%'")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        logger.warning("[ad_lookup] Could not read app_config from DB: %s", e)
        return {}


def _get_ad_config() -> dict:
    """
    Returns AD config dict with keys: server, port, base_dn, domain, username, password, use_ssl.
    Reads from env vars first; falls back to app_config table.
    Returns empty dict if neither source has AD_SERVER / ad.server.
    """
    server = os.getenv("AD_SERVER", "")
    if server:
        return {
            "server": server,
            "port": int(os.getenv("AD_PORT", "389")),
            "base_dn": os.getenv("AD_BASE_DN", ""),
            "domain": os.getenv("AD_DOMAIN", ""),
            "username": os.getenv("AD_USERNAME", ""),
            "password": os.getenv("AD_PASSWORD", ""),
            "use_ssl": os.getenv("AD_USE_SSL", "false").lower() == "true",
        }

    cfg = _load_ad_config_from_db()
    if not cfg.get("ad.server"):
        return {}
    return {
        "server": cfg.get("ad.server", ""),
        "port": int(cfg.get("ad.port", "389")),
        "base_dn": cfg.get("ad.base_dn", ""),
        "domain": cfg.get("ad.domain", ""),
        "username": cfg.get("ad.username", ""),
        "password": cfg.get("ad.password", ""),
        "use_ssl": cfg.get("ad.use_ssl", "false") == "true",
    }


def lookup_user(identifier: str) -> dict:
    """
    Looks up a user in Active Directory.

    Args:
        identifier: sAMAccountName or email

    Returns:
        {"success": True, "display_name": str, "email": str, "sam_account": str}
        {"success": False, "error": str}

    When AD is not configured, accepts the identifier as-is so the portal
    remains functional during initial setup (logs a warning).
    """
    if not identifier.strip():
        return {"success": False, "error": "Empty input"}

    ad_config = _get_ad_config()
    if not ad_config:
        return {"success": False, "error": "Active Directory is not configured. Configure AD via Admin > Settings > Active Directory."}

    return _msldap_lookup_sync(identifier, ad_config)


def lookup_manager(identifier: str) -> dict:
    """
    Looks up the manager of a user in Active Directory.

    Args:
        identifier: sAMAccountName or email of the user whose manager to look up

    Returns:
        {"success": True, "manager": {"email": str, "display_name": str, "sam_account": str}}
        {"success": True, "manager": None}  -- user found but no manager set
        {"success": False, "error": str}
    """
    if not identifier.strip():
        return {"success": False, "error": "Empty input"}

    ad_config = _get_ad_config()
    if not ad_config:
        return {"success": False, "error": "Active Directory is not configured. Configure AD via Admin > Settings > Active Directory."}

    return _msldap_manager_sync(identifier, ad_config)


def _msldap_manager_sync(identifier: str, ad_config: dict) -> dict:
    """Synchronous wrapper around the async manager lookup."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _msldap_lookup_manager(identifier, ad_config))
                return future.result(timeout=15)
        else:
            return asyncio.run(_msldap_lookup_manager(identifier, ad_config))
    except Exception as e:
        logger.error("AD manager lookup failed for '%s': %s", identifier, e)
        return {"success": False, "error": f"AD connection error: {e}"}


async def _msldap_lookup_manager(identifier: str, ad_config: dict) -> dict:
    """Look up a user's manager DN, then resolve the manager's identity."""
    from msldap.commons.factory import LDAPConnectionFactory
    from urllib.parse import quote

    server_host = ad_config["server"]
    server_port = ad_config["port"]
    base_dn = ad_config["base_dn"]
    bind_user = ad_config["username"]
    bind_password = ad_config["password"]
    domain = ad_config["domain"]
    use_ssl = ad_config.get("use_ssl", False)

    if "@" in identifier:
        ldap_filter = f"(|(mail={identifier})(userPrincipalName={identifier}))"
    else:
        sam = identifier.split("\\")[-1] if "\\" in identifier else identifier
        ldap_filter = f"(sAMAccountName={sam})"

    scheme = "ldaps+ntlm-password" if use_ssl else "ldap+ntlm-password"
    user_escaped = quote(f"{domain}\\{bind_user}" if domain else bind_user, safe="")
    pass_escaped = quote(bind_password, safe="")
    url = f"{scheme}://{user_escaped}:{pass_escaped}@{server_host}:{server_port}"

    factory = LDAPConnectionFactory.from_url(url)
    client = factory.get_client()
    await client.connect()

    try:
        # Step 1: Find user and get manager DN
        attrs = ["manager"]
        found = None
        async for entry, err in client.pagedsearch(ldap_filter, attrs, tree=base_dn):
            if err:
                return {"success": False, "error": str(err)}
            found = entry["attributes"]
            break

        if found is None:
            return {"success": False, "error": f"User '{identifier}' not found in AD"}

        manager_dn = found.get("manager")
        if isinstance(manager_dn, list):
            manager_dn = manager_dn[0] if manager_dn else None
        if manager_dn:
            manager_dn = str(manager_dn)

        if not manager_dn:
            return {"success": True, "manager": None}

        # Step 2: Resolve manager DN to get their identity
        mgr_attrs = ["mail", "displayName", "sAMAccountName", "userPrincipalName"]
        mgr_filter = f"(distinguishedName={manager_dn})"
        mgr_found = None
        async for entry, err in client.pagedsearch(mgr_filter, mgr_attrs, tree=base_dn):
            if err:
                return {"success": False, "error": f"Manager DN lookup error: {err}"}
            mgr_found = entry["attributes"]
            break

        if not mgr_found:
            return {"success": True, "manager": None}

        def _attr(key):
            v = mgr_found.get(key)
            if isinstance(v, list):
                return v[0] if v else None
            return str(v) if v else None

        return {
            "success": True,
            "manager": {
                "email": _attr("mail") or _attr("userPrincipalName") or "",
                "display_name": _attr("displayName") or "",
                "sam_account": _attr("sAMAccountName") or "",
            },
        }
    finally:
        await client.disconnect()


def check_group_membership(identifier: str, group_dn: str) -> dict:
    """
    Check if a user is a member of an AD group (recursive/transitive).

    Args:
        identifier: sAMAccountName or email of the user
        group_dn: Distinguished Name of the group

    Returns:
        {"success": True, "is_member": bool}
        {"success": False, "error": str}

    When AD is not configured (dev mode), always returns is_member=True.
    """
    if not identifier.strip() or not group_dn.strip():
        return {"success": False, "error": "Empty input"}

    ad_config = _get_ad_config()
    if not ad_config:
        return {"success": False, "error": "Active Directory is not configured. Configure AD via Admin > Settings > Active Directory."}

    return _msldap_check_membership_sync(identifier, group_dn, ad_config)


def _msldap_check_membership_sync(identifier: str, group_dn: str, ad_config: dict) -> dict:
    """Synchronous wrapper around the async membership check."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _msldap_check_membership(identifier, group_dn, ad_config))
                return future.result(timeout=15)
        else:
            return asyncio.run(_msldap_check_membership(identifier, group_dn, ad_config))
    except Exception as e:
        logger.error("AD group membership check failed for '%s' in '%s': %s", identifier, group_dn, e)
        return {"success": False, "error": f"AD connection error: {e}"}


async def _msldap_check_membership(identifier: str, group_dn: str, ad_config: dict) -> dict:
    """Check transitive group membership using LDAP_MATCHING_RULE_IN_CHAIN."""
    from msldap.commons.factory import LDAPConnectionFactory
    from urllib.parse import quote

    server_host = ad_config["server"]
    server_port = ad_config["port"]
    base_dn = ad_config["base_dn"]
    bind_user = ad_config["username"]
    bind_password = ad_config["password"]
    domain = ad_config["domain"]
    use_ssl = ad_config.get("use_ssl", False)

    # Build filter to find the user AND check transitive membership in one query.
    # OID 1.2.840.113556.1.4.1941 = LDAP_MATCHING_RULE_IN_CHAIN (recursive memberOf)
    if "@" in identifier:
        user_part = f"(|(mail={identifier})(userPrincipalName={identifier}))"
    else:
        sam = identifier.split("\\")[-1] if "\\" in identifier else identifier
        user_part = f"(sAMAccountName={sam})"

    ldap_filter = f"(&{user_part}(memberOf:1.2.840.113556.1.4.1941:={group_dn}))"

    scheme = "ldaps+ntlm-password" if use_ssl else "ldap+ntlm-password"
    user_escaped = quote(f"{domain}\\{bind_user}" if domain else bind_user, safe="")
    pass_escaped = quote(bind_password, safe="")
    url = f"{scheme}://{user_escaped}:{pass_escaped}@{server_host}:{server_port}"

    factory = LDAPConnectionFactory.from_url(url)
    client = factory.get_client()
    await client.connect()

    try:
        found = False
        async for entry, err in client.pagedsearch(ldap_filter, ["sAMAccountName"], tree=base_dn):
            if err:
                return {"success": False, "error": str(err)}
            found = True
            break

        return {"success": True, "is_member": found}
    finally:
        await client.disconnect()


def _msldap_lookup_sync(identifier: str, ad_config: dict) -> dict:
    """Synchronous wrapper around the async msldap lookup."""
    try:
        # Get or create an event loop — handles both threaded and main-thread contexts
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside an async context (e.g. FastAPI) — run in a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _msldap_lookup(identifier, ad_config))
                return future.result(timeout=15)
        else:
            return asyncio.run(_msldap_lookup(identifier, ad_config))
    except Exception as e:
        logger.error("AD lookup failed for '%s': %s", identifier, e)
        return {"success": False, "error": f"AD connection error: {e}"}


async def _msldap_lookup(identifier: str, ad_config: dict) -> dict:
    from msldap.commons.factory import LDAPConnectionFactory
    from urllib.parse import quote

    server_host = ad_config["server"]
    server_port = ad_config["port"]
    base_dn = ad_config["base_dn"]
    bind_user = ad_config["username"]
    bind_password = ad_config["password"]
    domain = ad_config["domain"]
    use_ssl = ad_config.get("use_ssl", False)

    # Build LDAP filter
    if "@" in identifier:
        ldap_filter = f"(|(mail={identifier})(userPrincipalName={identifier}))"
    else:
        sam = identifier.split("\\")[-1] if "\\" in identifier else identifier
        ldap_filter = f"(sAMAccountName={sam})"

    # Build msldap connection URL
    scheme = "ldaps+ntlm-password" if use_ssl else "ldap+ntlm-password"
    user_escaped = quote(f"{domain}\\{bind_user}" if domain else bind_user, safe="")
    pass_escaped = quote(bind_password, safe="")
    url = f"{scheme}://{user_escaped}:{pass_escaped}@{server_host}:{server_port}"

    factory = LDAPConnectionFactory.from_url(url)
    client = factory.get_client()
    await client.connect()

    try:
        attrs = ["mail", "displayName", "sAMAccountName", "userPrincipalName"]
        found = None
        async for entry, err in client.pagedsearch(ldap_filter, attrs, tree=base_dn):
            if err:
                return {"success": False, "error": str(err)}
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
            "email": _attr("mail") or _attr("userPrincipalName") or identifier,
            "display_name": _attr("displayName") or identifier,
            "sam_account": _attr("sAMAccountName") or identifier,
        }
    finally:
        await client.disconnect()
