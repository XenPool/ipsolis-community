"""Module Registry – central list of all available modules for the dynamic runner.

Jedes Modul hat:
- fn:          Aufrufbare Funktion
- needs_db:    True wenn die Funktion eine DB-Session als erstes Argument erwartet
- description: short description for the admin UI
- params:      Liste der erwarteten Parameter-Namen (aus params_template)
- output_keys: keys in the result dict that are carried into the execution context
- group:       grouping for admin UI dropdowns
"""

from tasks.modules import active_roles, notifications, pool_manager, sccm, target_executor, vsphere


# ── Notification adapter functions ───────────────────────────────────────────
# The original functions have complex signatures; these adapters take
# flat kwargs and delegate correctly.

def _notify_send_confirmation(
    db,
    user_email: str,
    user_name: str,
    owner_email: str | None = None,
    owner_name: str | None = None,
    asset_type_name: str = "",
    asset_type_description: str | None = None,
    requested_from=None,
    expires_at=None,
    snow_req: str | None = None,
    snow_ritm: str | None = None,
) -> dict:
    from datetime import datetime, timezone
    if requested_from is None:
        requested_from = datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = datetime.now(timezone.utc)
    return notifications.send_order_confirmation(
        db=db,
        user_email=user_email or "",
        user_name=user_name or "",
        owner_email=owner_email,
        owner_name=owner_name,
        asset_type_name=asset_type_name or "",
        asset_type_description=asset_type_description or "",
        requested_from=requested_from,
        requested_until=expires_at,
        snow_req=snow_req,
        snow_ritm=snow_ritm,
    )


def _notify_send_provision(
    db,
    user_email: str,
    user_name: str,
    asset_name: str,
    rdp_users: list | None = None,
    expires_at=None,
) -> dict:
    from datetime import datetime, timezone
    if expires_at is None:
        expires_at = datetime.now(timezone.utc)
    return notifications.send_provision_confirmation(
        db=db,
        user_email=user_email or "",
        user_name=user_name or "",
        asset_name=asset_name or "",
        rdp_users=rdp_users or [],
        expires_at=expires_at,
    )


def _notify_send_reclaim(
    db,
    user_email: str,
    user_name: str,
    asset_name: str,
) -> dict:
    return notifications.send_reclaim_notification(
        db=db,
        user_email=user_email or "",
        user_name=user_name or "",
        asset_name=asset_name or "",
    )


# ── Registry ──────────────────────────────────────────────────────────────────

MODULE_REGISTRY: dict[str, dict] = {

    # ── Pool ──────────────────────────────────────────────────────────────────
    "pool.reserve_asset": {
        "fn": pool_manager.reserve_asset,
        "needs_db": True,
        "description": "Reserves a free asset from the pool for the order",
        "params": ["order_id", "asset_type_id", "expires_at"],
        "output_keys": ["asset_id", "asset_name"],
        "group": "pool",
    },
    "pool.check_capacity": {
        "fn": pool_manager.check_capacity,
        "needs_db": True,
        "description": "Checks whether pool capacity for pooled assets is still available",
        "params": ["asset_type_id", "pool_capacity"],
        "output_keys": [],
        "group": "pool",
    },
    "pool.set_asset_busy": {
        "fn": pool_manager.set_asset_busy,
        "needs_db": True,
        "description": "Sets an asset to BUSY (after provisioning or extension)",
        "params": ["asset_id", "order_id", "expires_at"],
        "output_keys": [],
        "group": "pool",
    },
    "pool.release_asset": {
        "fn": pool_manager.release_asset,
        "needs_db": True,
        "description": "Returns an asset to the pool (status: FREE)",
        "params": ["asset_id"],
        "output_keys": [],
        "group": "pool",
    },

    # ── Active Roles ──────────────────────────────────────────────────────────
    "active_roles.set_rdp_group": {
        "fn": active_roles.set_rdp_group,
        "needs_db": False,
        "description": "Populates the RDP AD group of the VM with the specified users",
        "params": ["asset_name", "rdp_users"],
        "output_keys": [],
        "group": "active_roles",
    },
    "active_roles.set_admin_group": {
        "fn": active_roles.set_admin_group,
        "needs_db": False,
        "description": "Populates the admin AD group of the VM with the specified users",
        "params": ["asset_name", "admin_users"],
        "output_keys": [],
        "group": "active_roles",
    },
    "active_roles.remove_all_groups": {
        "fn": active_roles.remove_all_groups,
        "needs_db": False,
        "description": "Removes all AD groups of the VM (on return)",
        "params": ["asset_name"],
        "output_keys": [],
        "group": "active_roles",
    },

    # ── vSphere ───────────────────────────────────────────────────────────────
    "vsphere.update_vmware_tools": {
        "fn": vsphere.update_vmware_tools,
        "needs_db": False,
        "description": "Aktualisiert VMware Tools auf der VM via PowerCLI",
        "params": ["asset_name"],
        "output_keys": [],
        "group": "vsphere",
    },
    "vsphere.restart_vm": {
        "fn": vsphere.restart_vm,
        "needs_db": False,
        "description": "Startet die VM neu via vSphere",
        "params": ["asset_name"],
        "output_keys": [],
        "group": "vsphere",
    },

    # ── SCCM ──────────────────────────────────────────────────────────────────
    "sccm.trigger_reinstall": {
        "fn": sccm.trigger_reinstall,
        "needs_db": False,
        "description": "Triggers SCCM task sequence for unattended VM reinstallation",
        "params": ["asset_name"],
        "output_keys": [],
        "group": "sccm",
    },

    # ── Notifications ─────────────────────────────────────────────────────────
    "notifications.send_confirmation": {
        "fn": _notify_send_confirmation,
        "needs_db": True,
        "description": "Sends bilingual order confirmation to requester and owner",
        "params": [
            "user_email", "user_name", "owner_email", "owner_name",
            "asset_type_name", "asset_type_description",
            "requested_from", "expires_at", "snow_req", "snow_ritm",
        ],
        "output_keys": [],
        "group": "notifications",
    },
    "notifications.send_provision_confirmation": {
        "fn": _notify_send_provision,
        "needs_db": True,
        "description": "Sends provisioning confirmation with VM name and RDP access",
        "params": ["user_email", "user_name", "asset_name", "rdp_users", "expires_at"],
        "output_keys": [],
        "group": "notifications",
    },
    "notifications.send_reclaim": {
        "fn": _notify_send_reclaim,
        "needs_db": True,
        "description": "Notifies user about their VM being returned to the pool",
        "params": ["user_email", "user_name", "asset_name"],
        "output_keys": [],
        "group": "notifications",
    },

    # ── Target Executor ───────────────────────────────────────────────────────
    "target_executor.grant": {
        "fn": target_executor.grant,
        "needs_db": True,
        "description": "Reads targets from asset_types and adds principals to groups (AD/Entra)",
        "params": ["order_id", "asset_type_id", "user_email", "rdp_users", "admin_users"],
        "output_keys": [],
        "group": "target_executor",
    },
    "target_executor.revoke": {
        "fn": target_executor.revoke,
        "needs_db": True,
        "description": "Inverts all grant entries from the change log (deterministic revoke)",
        "params": ["user_email", "asset_type_id"],
        "output_keys": [],
        "group": "target_executor",
    },
}
