"""Modul: Target Executor – Config-driven Gruppen-Zugriff.

Reads 'targets' from asset_types and adds/removes principals from groups.
Writes deterministic order change log for revoke.

targets-Format (JSONB in asset_types):
    [{"type": "ad_group", "identifier": "CN=App-Users,OU=...", "principal_source": "requester"}]

principal_source-Werte:
    "requester"    – user_email des Antragstellers
    "rdp_users"    – rdp_users-Liste aus der Order
    "admin_users"  – admin_users-Liste aus der Order
    "all_users"    – alle drei kombiniert
"""

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from tasks.modules.config_reader import get_config, get_config_int

logger = logging.getLogger(__name__)


# ── LDAP helpers (msldap – supports NTLM signing for modern Windows Server) ─────

def _build_msldap_url(db: Session) -> tuple[str, str]:
    """Returns (msldap_url, base_dn) using ad.* keys from app_config."""
    from urllib.parse import quote

    server_host = get_config(db, "ad.server", "dc.example.com")
    server_port = get_config_int(db, "ad.port", 389)
    bind_user = get_config(db, "ad.username", "")
    bind_password = get_config(db, "ad.password", "")
    domain = get_config(db, "ad.domain", "")
    base_dn = get_config(db, "ad.base_dn", "DC=example,DC=com")

    raw_user = f"{domain}\\{bind_user}" if domain else bind_user
    url = (f"ldap+ntlm-password://{quote(raw_user, safe='')}:"
           f"{quote(bind_password, safe='')}@{server_host}:{server_port}")
    return url, base_dn


async def _resolve_dn_async(principal: str, client, base_dn: str) -> str:
    """Resolves an email or sAMAccountName to the user's full DN in AD."""
    if "@" in principal:
        # Try mail first, fall back to UPN (userPrincipalName) — covers users with no mail attribute
        ldap_filter = f"(|(mail={principal})(userPrincipalName={principal}))"
    else:
        sam = principal.split("\\")[-1] if "\\" in principal else principal
        ldap_filter = f"(sAMAccountName={sam})"

    async for entry, err in client.pagedsearch(ldap_filter, ["distinguishedName"], tree=base_dn):
        if err:
            raise ValueError(f"LDAP search error: {err}")
        dn = entry["attributes"].get("distinguishedName")
        if isinstance(dn, list):
            dn = dn[0]
        if dn:
            return dn
    raise ValueError(f"User '{principal}' not found in AD")


# ── Principal resolution ────────────────────────────────────────────────────────

def _resolve_principals(
    principal_source: str,
    user_email: str,
    rdp_users: list,
    admin_users: list,
) -> list[str]:
    """Returns the list of affected user principals."""
    if principal_source == "requester":
        return [user_email] if user_email else []
    if principal_source == "rdp_users":
        return list(rdp_users or [])
    if principal_source == "admin_users":
        return list(admin_users or [])
    if principal_source == "all_users":
        principals: set[str] = set()
        if user_email:
            principals.add(user_email)
        principals.update(rdp_users or [])
        principals.update(admin_users or [])
        return list(principals)
    return []


# ── Change-Log Helper ──────────────────────────────────────────────────────────

def _write_change_log(
    db: Session,
    order_id: int,
    target_type: str,
    identifier: str,
    action: str,
    principal: str,
    state: str,
    metadata: dict | None = None,
    idempotency_key: str | None = None,
    resolved_object_id: str | None = None,
) -> None:
    db.execute(
        text("""
            INSERT INTO order_change_log
                (order_id, target_type, identifier, action, principal, state,
                 executed_at, metadata, idempotency_key, resolved_object_id)
            VALUES (:oid, :ttype, :ident, :action, :principal, :state,
                    NOW(), CAST(:meta AS jsonb), :ikey, :robj)
        """),
        {
            "oid": order_id,
            "ttype": target_type,
            "ident": identifier,
            "action": action,
            "principal": principal,
            "state": state,
            "meta": json.dumps(metadata) if metadata else "null",
            "ikey": idempotency_key,
            "robj": resolved_object_id,
        },
    )
    db.commit()


# ── Handler-Funktionen ─────────────────────────────────────────────────────────

def _grant_ad_group(identifier: str, principal: str, db: Session) -> dict:
    """Adds principal to the AD group identified by its DN."""
    import asyncio
    from msldap.commons.factory import LDAPConnectionFactory

    url, base_dn = _build_msldap_url(db)

    async def _do():
        factory = LDAPConnectionFactory.from_url(url)
        client = factory.get_client()
        await client.connect()
        try:
            user_dn = await _resolve_dn_async(principal, client, base_dn)
            _, err = await client.add_user_to_group(user_dn, identifier)
            if err and "already" not in str(err).lower() and "exists" not in str(err).lower():
                raise RuntimeError(f"LDAP add_user_to_group failed: {err}")
            return user_dn
        finally:
            await client.disconnect()

    user_dn = asyncio.run(_do())
    logger.info("AD group grant OK: %s → %s (dn=%s)", principal, identifier, user_dn)
    return {"success": True, "user_dn": user_dn}


def _grant_entra_group(identifier: str, principal: str, db: Session) -> dict:
    """Adds principal to the Entra group identified by identifier (MS Graph)."""
    raise NotImplementedError("Entra group grant not yet implemented")


def _revoke_ad_group(identifier: str, principal: str, db: Session) -> dict:
    """Removes principal from the AD group identified by its DN."""
    import asyncio
    from msldap.commons.factory import LDAPConnectionFactory

    url, base_dn = _build_msldap_url(db)

    async def _do():
        factory = LDAPConnectionFactory.from_url(url)
        client = factory.get_client()
        await client.connect()
        try:
            user_dn = await _resolve_dn_async(principal, client, base_dn)
            _, err = await client.del_user_from_group(user_dn, identifier)
            if err:
                err_s = str(err).lower()
                # Treat "already not a member" as success (idempotent)
                # AD error 0x561 (WILL_NOT_PERFORM / problem 5003) = user not in group
                if not any(s in err_s for s in ("no such", "not a member", "will_not_perform", "0x561", "5003")):
                    raise RuntimeError(f"LDAP del_user_from_group failed: {err}")
            return user_dn
        finally:
            await client.disconnect()

    user_dn = asyncio.run(_do())
    logger.info("AD group revoke OK: %s ← %s (dn=%s)", principal, identifier, user_dn)
    return {"success": True, "user_dn": user_dn}


def _revoke_entra_group(identifier: str, principal: str, db: Session) -> dict:
    """Removes principal from Entra group identifier."""
    raise NotImplementedError("Entra group revoke not yet implemented")


_GRANT_HANDLERS: dict[str, object] = {
    "ad_group": _grant_ad_group,
    "entra_group": _grant_entra_group,
}

_REVOKE_HANDLERS: dict[str, object] = {
    "ad_group": _revoke_ad_group,
    "entra_group": _revoke_entra_group,
}


# ── Public module functions ───────────────────────────────────────────────

def grant(
    db: Session,
    order_id: int,
    asset_type_id: int,
    user_email: str,
    rdp_users: list | None = None,
    admin_users: list | None = None,
) -> dict:
    """Reads targets from asset_types, adds principals to groups,
    writes order change log.

    Returns:
        {"success": True, "grants": n, "mock": bool}
        {"success": False, "grants": n, "errors": [...]}
    """
    row = db.execute(
        text("SELECT targets FROM asset_types WHERE id = :id"),
        {"id": asset_type_id},
    ).fetchone()

    if not row or not row[0]:
        logger.info("[target_executor] No targets defined for asset_type_id=%s", asset_type_id)
        return {"success": True, "grants": 0}

    targets: list[dict] = row[0]
    granted = 0
    errors: list[str] = []

    for target in targets:
        target_type = target.get("type", "")
        identifier = target.get("identifier", "")
        principal_source = target.get("principal_source", "requester")

        principals = _resolve_principals(
            principal_source,
            user_email or "",
            rdp_users or [],
            admin_users or [],
        )

        handler = _GRANT_HANDLERS.get(target_type)
        if not handler:
            logger.warning("[target_executor] Unknown target type: %s", target_type)
            _write_change_log(
                db, order_id, target_type, identifier, "grant",
                principal="(unknown)", state="failed",
                metadata={"error": "Unknown target type: " + target_type},
            )
            continue

        for principal in principals:
            ikey = f"order-{order_id}-{target_type}-{identifier}-{principal}"

            # Idempotency check: grant already executed successfully → skip
            existing = db.execute(
                text("""
                    SELECT 1 FROM order_change_log
                    WHERE idempotency_key = :k AND state = 'success'
                    LIMIT 1
                """),
                {"k": ikey},
            ).fetchone()
            if existing:
                logger.info("[target_executor] Skipping duplicate grant (idempotent): %s", ikey)
                granted += 1
                continue

            try:
                result = handler(identifier, principal, db)
                state = "success" if result.get("success") else "failed"
                _write_change_log(
                    db, order_id, target_type, identifier, "grant",
                    principal=principal, state=state,
                    metadata={},
                    idempotency_key=ikey,
                )
                if state == "success":
                    granted += 1
                else:
                    errors.append("grant " + target_type + ":" + identifier + " for " + principal + " failed")
            except Exception as e:
                logger.error(
                    "[target_executor] grant error: %s:%s principal=%s – %s",
                    target_type, identifier, principal, e,
                )
                _write_change_log(
                    db, order_id, target_type, identifier, "grant",
                    principal=principal, state="failed",
                    metadata={"error": str(e)},
                    idempotency_key=ikey,
                )
                errors.append(str(e))

    if errors:
        return {"success": False, "grants": granted, "errors": errors}
    return {"success": True, "grants": granted}


def revoke(
    db: Session,
    user_email: str,
    asset_type_id: int,
) -> dict:
    """Finds all successful grant entries for user_email + asset_type_id
    in the order change log and inverts them deterministically.

    Sets state = 'rolled_back' for successfully rolled back entries.

    Returns:
        {"success": True, "revokes": n, "mock": bool}
        {"success": False, "revokes": n, "errors": [...]}
    """
    rows = db.execute(
        text("""
            SELECT cl.id, cl.target_type, cl.identifier, cl.principal
            FROM order_change_log cl
            JOIN orders o ON o.id = cl.order_id
            WHERE o.user_email = :email
              AND o.asset_type_id = :at
              AND cl.action = 'grant'
              AND cl.state = 'success'
            ORDER BY cl.id DESC
        """),
        {"email": user_email, "at": asset_type_id},
    ).fetchall()

    if not rows:
        logger.info(
            "[target_executor] No change log entries to revoke for user=%s asset_type=%s",
            user_email, asset_type_id,
        )
        return {"success": True, "revokes": 0}

    revoked = 0
    errors: list[str] = []

    for row in rows:
        log_id, target_type, identifier, principal = row[0], row[1], row[2], row[3]
        handler = _REVOKE_HANDLERS.get(target_type)

        if not handler:
            logger.warning("[target_executor] No revoke handler for type: %s", target_type)
            db.execute(
                text("UPDATE order_change_log SET state = 'failed' WHERE id = :id"),
                {"id": log_id},
            )
            db.commit()
            continue

        try:
            result = handler(identifier, principal, db)
            new_state = "rolled_back" if result.get("success") else "failed"
            db.execute(
                text("UPDATE order_change_log SET state = :state WHERE id = :id"),
                {"state": new_state, "id": log_id},
            )
            db.commit()
            if new_state == "rolled_back":
                revoked += 1
            else:
                errors.append("revoke " + target_type + ":" + identifier + " for " + principal + " failed")
        except Exception as e:
            logger.error(
                "[target_executor] revoke error: %s:%s principal=%s – %s",
                target_type, identifier, principal, e,
            )
            db.execute(
                text("UPDATE order_change_log SET state = 'failed' WHERE id = :id"),
                {"id": log_id},
            )
            db.commit()
            errors.append(str(e))

    if errors:
        return {"success": False, "revokes": revoked, "errors": errors}
    return {"success": True, "revokes": revoked}
