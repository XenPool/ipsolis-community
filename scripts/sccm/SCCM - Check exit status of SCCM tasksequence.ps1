# SCCM - Check exit status of SCCM tasksequence

# Import the SCCM module
Import-module "C:\Windows\System32\WindowsPowerShell\v1.0\Modules\SCCM\bin\ConfigurationManager.psd1"

# Convert the plain text password to a secure string
$secpasswd = ConvertTo-SecureString '^[SCCMPW]' -AsPlainText -Force

# Create a credential object using the username and secure password
$cred = New-Object System.Management.Automation.PSCredential("^[SCCMUser]", $secpasswd)

$initParams = @{}
$depStatus = @{}

# Check if the PSDrive for the SCCM site exists, if not, create it
if ($null -eq (Get-PSDrive -Name '^[SCCMSite]' -PSProvider CMSite -ErrorAction SilentlyContinue)) {
    New-PSDrive -Name '^[SCCMSite]' -PSProvider CMSite -Root '^[SCCMSiteServer]' -Credential $cred @initParams
}

# Set the location to the SCCM site
Set-Location "$('^[SCCMSite]'):\" @initParams

# Get the name of the OS collection using its ID
$OSCollectionName = (Get-CMDeviceCollection -Id "$[OSCollectionID]").Name

# Get the deployment ID for the OS collection
$depID = (Get-CMDeployment -CollectionName $OSCollectionName).DeploymentID

$loops = 1

# Loop to check the deployment status every minute, up to 360 times
while (($depStatus.StatusDescription -ne "The task sequence manager successfully completed execution of the task sequence") -and ($loops -le "360")) {
    $depStatus = (Get-CMDeploymentStatus -DeploymentId $depID | Get-CMDeploymentStatusDetails | Where-Object DeviceName -eq "$[VMName]")
    Write-Host $(Get-Date -Format 'HH:mm') => "status of tasksequence: " $depStatus.StatusDescription loop $loops of 360
    $loops++
    Start-Sleep -s 60
}

# Check if the task sequence completed successfully
if ($depStatus.StatusDescription -eq "The task sequence manager successfully completed execution of the task sequence") {
    Write-Host Tasksequence has ended successfully! :-')'
    Write-Host "Last deployment status of $[VMName]:" $depStatus.StatusDescription
    Write-Host "Installation of $[VMName] took around:" $loops "minutes"
    $Global:SCCMLastStatus = $depStatus.StatusDescription
    $Global:TaskSequenceResult = "Available"
    $Global:DeploymentID = $depID
} else {
    Write-Host Tasksequence has ended with errors! :-'('
    Write-Host "Last deployment status was $[VMName]:" $depStatus.StatusDescription
    $Global:TaskSequenceResult = "TaskSeqRunError"
    $Global:DeploymentID = $depID
}