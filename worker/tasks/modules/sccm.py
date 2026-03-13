"""Modul: SCCM – Unattended Reinstall-Trigger via WinRM.

Entspricht dem Ivanti-Modul 'SCCM-Trigger'.
"""

import logging
import os

logger = logging.getLogger(__name__)

SCCM_TASK_SEQUENCE_ID = os.getenv("SCCM_TASK_SEQUENCE_ID", "TSQ00001")
SCCM_SITE_CODE = os.getenv("SCCM_SITE_CODE", "XP1")


def trigger_reinstall(asset_name: str) -> dict:
    """
    Triggers an SCCM unattended reinstall task sequence for the VM.

    Nach dem Trigger: VM wird reinstalliert und ist danach wieder FREE im Pool.
    """
    return _production_trigger(asset_name)


def check_reinstall_status(asset_name: str) -> dict:
    """Checks whether the SCCM reinstallation is complete."""
    return _production_check_status(asset_name)


def _production_trigger(asset_name: str) -> dict:
    """Production: WinRM-Aufruf zum SCCM-Server."""
    try:
        import pypsrp
        from pypsrp.client import Client

        sccm_host = os.getenv("SCCM_WINRM_HOST", "")
        sccm_user = os.getenv("SCCM_WINRM_USER", "")
        sccm_password = os.getenv("SCCM_WINRM_PASSWORD", "")

        ps_script = f"""
            $SiteCode = '{SCCM_SITE_CODE}'
            $TaskSequenceID = '{SCCM_TASK_SEQUENCE_ID}'
            $ComputerName = '{asset_name}'

            Import-Module 'C:\\Program Files (x86)\\Microsoft Configuration Manager\\AdminConsole\\bin\\ConfigurationManager.psd1'
            Set-Location "$($SiteCode):"

            $Advertisement = Get-CMTaskSequenceDeployment -TaskSequenceId $TaskSequenceID
            Invoke-CMDeployment -DeploymentId $Advertisement.AdvertisementID -ComputerName $ComputerName
            Write-Output (ConvertTo-Json @{{success=$true; computer=$ComputerName}})
        """

        with Client(sccm_host, username=sccm_user, password=sccm_password, ssl=False) as client:
            output, streams, had_errors = client.execute_ps(ps_script)
            if had_errors:
                return {"success": False, "error": str(streams.error)}
            return {"success": True, "output": output}

    except Exception as e:
        return {"success": False, "error": str(e)}


def _production_check_status(asset_name: str) -> dict:
    return {"success": True, "status": "unknown", "message": "Not yet implemented"}

