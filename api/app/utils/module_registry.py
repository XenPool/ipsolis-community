"""Metadata mirror of the module registry for the admin UI.

Contains the same structure as worker/tasks/modules/registry.py,
but without function references – for UI dropdowns and documentation only.
"""

# Grouped module metadata (no import from worker – API has no access)
MODULE_METADATA: list[dict] = [
    # ── Pool ──────────────────────────────────────────────────────────────────
    {
        "key": "pool.reserve_asset",
        "group": "pool",
        "description": "Reserves a free asset from the pool for the order",
        "params": ["order_id", "asset_type_id", "expires_at"],
        "output_keys": ["asset_id", "asset_name"],
    },
    {
        "key": "pool.check_capacity",
        "group": "pool",
        "description": "Checks whether pool capacity for pooled assets is still available",
        "params": ["asset_type_id", "pool_capacity"],
        "output_keys": [],
    },
    {
        "key": "pool.set_asset_busy",
        "group": "pool",
        "description": "Sets an asset to BUSY (after provisioning or extension)",
        "params": ["asset_id", "order_id", "expires_at"],
        "output_keys": [],
    },
    {
        "key": "pool.release_asset",
        "group": "pool",
        "description": "Returns an asset to the pool (status: FREE)",
        "params": ["asset_id"],
        "output_keys": [],
    },
    # ── vSphere ───────────────────────────────────────────────────────────────
    {
        "key": "vsphere.update_vmware_tools",
        "group": "vsphere",
        "description": "Aktualisiert VMware Tools auf der VM via PowerCLI",
        "params": ["asset_name"],
        "output_keys": [],
    },
    {
        "key": "vsphere.restart_vm",
        "group": "vsphere",
        "description": "Startet die VM neu via vSphere",
        "params": ["asset_name"],
        "output_keys": [],
    },
    # ── SCCM ──────────────────────────────────────────────────────────────────
    {
        "key": "sccm.trigger_reinstall",
        "group": "sccm",
        "description": "Triggers SCCM task sequence for unattended VM reinstall",
        "params": ["asset_name"],
        "output_keys": [],
    },
    # ── Notifications ─────────────────────────────────────────────────────────
    {
        "key": "notifications.send_confirmation",
        "group": "notifications",
        "description": "Sends bilingual order confirmation to requester and owner",
        "params": [
            "user_email", "user_name", "owner_email", "owner_name",
            "asset_type_name", "asset_type_description",
            "requested_from", "expires_at", "snow_req", "snow_ritm",
            "scheduled_date",
        ],
        "output_keys": [],
    },
    {
        "key": "notifications.send_provision_confirmation",
        "group": "notifications",
        "description": "Sends provisioning confirmation with VM name and RDP access",
        "params": ["user_email", "user_name", "asset_name", "rdp_users", "expires_at"],
        "output_keys": [],
    },
    {
        "key": "notifications.send_reclaim",
        "group": "notifications",
        "description": "Notifies the user about the return of their VM to the pool",
        "params": ["user_email", "user_name", "asset_name"],
        "output_keys": [],
    },
    # ── Target Executor ───────────────────────────────────────────────────────
    {
        "key": "target_executor.grant",
        "group": "target_executor",
        "description": "Reads targets from asset_types and adds principals to groups (AD/Entra)",
        "params": ["order_id", "asset_type_id", "user_email", "rdp_users", "admin_users"],
        "output_keys": [],
    },
    {
        "key": "target_executor.revoke",
        "group": "target_executor",
        "description": "Inverts all grant entries from the change log (deterministic revoke)",
        "params": ["user_email", "asset_type_id"],
        "output_keys": [],
    },
]

# Index for fast lookup by key
MODULE_MAP: dict[str, dict] = {m["key"]: m for m in MODULE_METADATA}

# Group order for UI
MODULE_GROUPS = ["pool", "vsphere", "sccm", "notifications", "target_executor"]
