# SCCM - Delete Device in SCCM

# Import the SCCM module
Import-module "C:\Windows\System32\WindowsPowerShell\v1.0\Modules\SCCM\bin\ConfigurationManager.psd1"

# Convert the plain text password to a secure string
$secpasswd = ConvertTo-SecureString '^[SCCMPW]' -AsPlainText -Force

# Create a credential object using the username and secure password
$cred = New-Object System.Management.Automation.PSCredential("^[SCCMUser]", $secpasswd)

$initParams = @{}

# Check if the PSDrive for the SCCM site exists, if not, create it
if ($null -eq (Get-PSDrive -Name '^[SCCMSite]' -PSProvider CMSite -ErrorAction SilentlyContinue)) {
    New-PSDrive -Name '^[SCCMSite]' -PSProvider CMSite -Root '^[SCCMSiteServer]' -Credential $cred @initParams
}

# Set the location to the SCCM site
Set-Location "$('^[SCCMSite]'):\" @initParams

# Check if the VMName variable is not null
if ("$[VMName]" -ne $null) {
    # Get the device by name and remove it from SCCM
    Get-CMDevice -Name "$[VMName]" | Select-Object -Last 1 | Remove-CMDevice -Force
}