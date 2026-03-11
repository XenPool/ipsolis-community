"""Module: Active Roles – AD group management via WinRM/pypsrp.

Calls PowerShell scripts on the Active Roles host.
Corresponds to the Ivanti modules 'Set-ARGroups' and 'Remove-ARGroups'.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
SCRIPTS_DIR = Path("/app/scripts/active_roles")


def set_rdp_group(asset_name: str, rdp_users: list[str]) -> dict:
    """Populates the RDP AD group of the VM with the specified users."""
    if ENVIRONMENT == "development":
        return _mock_set_group("RDP", asset_name, rdp_users)

    return _run_ps_script("Set-ARGroups.ps1", {
        "VMName": asset_name,
        "GroupType": "RDP",
        "Users": rdp_users,
    })


def set_admin_group(asset_name: str, admin_users: list[str]) -> dict:
    """Populates the Admin AD group of the VM with the specified users."""
    if ENVIRONMENT == "development":
        return _mock_set_group("Admin", asset_name, admin_users)

    return _run_ps_script("Set-ARGroups.ps1", {
        "VMName": asset_name,
        "GroupType": "Admin",
        "Users": admin_users,
    })


def remove_all_groups(asset_name: str) -> dict:
    """Clears all AD groups of the VM (on return)."""
    if ENVIRONMENT == "development":
        return _mock_remove_groups(asset_name)

    return _run_ps_script("Remove-ARGroups.ps1", {"VMName": asset_name})


def _run_ps_script(script_name: str, params: dict) -> dict:
    """Executes a PowerShell script via pwsh and returns JSON output."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        return {"success": False, "error": f"Script not found: {script_path}"}

    # Pass parameters as JSON string
    params_json = json.dumps(params)
    cmd = ["pwsh", "-File", str(script_path), "-ParamsJson", params_json]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            return {
                "success": False,
                "error": result.stderr.strip() or f"Exit code {result.returncode}",
                "output": output,
            }
        parsed = json.loads(output) if output else {}
        return {"success": True, **parsed}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Script timeout after 120s"}
    except json.JSONDecodeError:
        return {"success": False, "error": f"Invalid JSON output: {output!r}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Mocks ─────────────────────────────────────────────────────────────────────

def _mock_set_group(group_type: str, asset_name: str, users: list[str]) -> dict:
    import time
    group_name = f"VDI-{'RDP' if group_type == 'RDP' else 'ADM'}-{asset_name}"
    logger.info(
        "[MOCK] Active Roles: Setting %s group '%s' with users: %s",
        group_type, group_name, users,
    )
    time.sleep(1.0)  # Simuliert WinRM-Latenz
    logger.info("[MOCK] Active Roles: Group '%s' updated successfully", group_name)
    return {"success": True, "group_name": group_name, "users_set": len(users)}


def _mock_remove_groups(asset_name: str) -> dict:
    import time
    logger.info("[MOCK] Active Roles: Removing all groups for VM '%s' ...", asset_name)
    time.sleep(1.0)
    logger.info("[MOCK] Active Roles: All groups cleared for '%s'", asset_name)
    return {"success": True, "groups_cleared": 2}
