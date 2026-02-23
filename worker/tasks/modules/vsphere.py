"""Modul: vSphere – VMware-Operationen via PowerCLI.

Führt PowerShell-Scripts mit PowerCLI aus.
Entspricht den Ivanti-Modulen 'Update-VMwareTools' und 'Restart-VM'.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
SCRIPTS_DIR = Path("/app/scripts/vsphere")


def update_vmware_tools(asset_name: str) -> dict:
    """Aktualisiert VMware Tools auf der VM."""
    if ENVIRONMENT == "development":
        return _mock_update_tools(asset_name)

    return _run_ps_script("Update-VMwareTools.ps1", {"VMName": asset_name})


def restart_vm(asset_name: str) -> dict:
    """Führt einen Reboot der VM durch und wartet auf Verfügbarkeit."""
    if ENVIRONMENT == "development":
        return _mock_restart_vm(asset_name)

    return _run_ps_script("Restart-VM.ps1", {"VMName": asset_name})


def get_vm_status(asset_name: str) -> dict:
    """Gibt den aktuellen Status der VM zurück."""
    if ENVIRONMENT == "development":
        return _mock_get_status(asset_name)

    return _run_ps_script("Get-VMStatus.ps1", {"VMName": asset_name})


def _run_ps_script(script_name: str, params: dict) -> dict:
    """Führt ein PowerCLI-Script via pwsh aus."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        return {"success": False, "error": f"Script not found: {script_path}"}

    params_json = json.dumps(params)
    cmd = ["pwsh", "-File", str(script_path), "-ParamsJson", params_json]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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
        return {"success": False, "error": "Script timeout after 300s"}
    except json.JSONDecodeError:
        return {"success": False, "error": f"Invalid JSON output: {output!r}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Mocks ─────────────────────────────────────────────────────────────────────

def _mock_update_tools(asset_name: str) -> dict:
    import time
    logger.info("[MOCK] vSphere: Updating VMware Tools on '%s' ...", asset_name)
    time.sleep(2.0)  # Simuliert Tools-Update-Laufzeit
    logger.info("[MOCK] vSphere: VMware Tools updated on '%s' (version: 12.3.5)", asset_name)
    return {"success": True, "tools_version": "12.3.5", "reboot_required": False}


def _mock_restart_vm(asset_name: str) -> dict:
    import time
    logger.info("[MOCK] vSphere: Initiating reboot of '%s' ...", asset_name)
    time.sleep(1.0)
    logger.info("[MOCK] vSphere: '%s' rebooting ...", asset_name)
    time.sleep(3.0)  # Simuliert Boot-Zeit
    logger.info("[MOCK] vSphere: '%s' is back online (Tools running)", asset_name)
    return {"success": True, "power_state": "poweredOn", "tools_status": "running"}


def _mock_get_status(asset_name: str) -> dict:
    logger.info("[MOCK] vSphere: Getting status of '%s'", asset_name)
    return {
        "success": True,
        "power_state": "poweredOn",
        "tools_status": "running",
        "ip_address": "10.100.50.42",
        "guest_os": "Windows Server 2022",
    }
