"""Modul: vSphere – VMware-Operationen via PowerCLI.

Executes PowerShell scripts via PowerCLI.
Corresponds to Ivanti modules 'Update-VMwareTools' and 'Restart-VM'.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path("/app/scripts/vsphere")


def update_vmware_tools(asset_name: str) -> dict:
    """Aktualisiert VMware Tools auf der VM."""
    return _run_ps_script("Update-VMwareTools.ps1", {"VMName": asset_name})


def restart_vm(asset_name: str) -> dict:
    """Reboots the VM and waits for availability."""
    return _run_ps_script("Restart-VM.ps1", {"VMName": asset_name})


def get_vm_status(asset_name: str) -> dict:
    """Returns the current status of the VM."""
    return _run_ps_script("Get-VMStatus.ps1", {"VMName": asset_name})


def _run_ps_script(script_name: str, params: dict) -> dict:
    """Executes a PowerCLI script via pwsh."""
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

