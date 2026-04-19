# SCCM - Create Device in SCCM and boot to PXE (ROBUST + PRO LOGGING + SAFETY STOPS)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

#region Logging
function Write-Log {
    param(
        [Parameter(Mandatory=$true)][string]$Message,
        [ValidateSet("DEBUG","INFO","WARN","ERROR")][string]$Level = "INFO",
        [string]$Context = "RUNBOOK"
    )

    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    $procId = $PID
    $lvl = $Level.PadRight(5)
    Write-Host "[$ts][$lvl][$Context][PID:$procId] $Message"
}
#endregion

#region Helpers
function Invoke-Retry {
    param(
        [Parameter(Mandatory=$true)][scriptblock]$ScriptBlock,
        [int]$MaxAttempts = 5,
        [int]$DelaySeconds = 3,
        [string]$Context = "RETRY",
        [string]$ActionName = "Action"
    )

    for ($attempt=1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            Write-Log "$ActionName (attempt $attempt/$MaxAttempts)..." "DEBUG" $Context
            return & $ScriptBlock
        }
        catch {
            Write-Log "$ActionName failed: $($_.Exception.Message)" "WARN" $Context
            if ($attempt -lt $MaxAttempts) {
                Start-Sleep -Seconds $DelaySeconds
            } else {
                throw
            }
        }
    }
}

function Test-NotEmpty([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Required parameter '$Name' is empty."
    }
}

function Convert-MacAddress([string]$mac) {
    if ([string]::IsNullOrWhiteSpace($mac)) { return $mac }
    $m = $mac.Trim()
    $m = $m -replace "[-\.]", ":" -replace "\s", ""
    if ($m -match "^[0-9A-Fa-f]{12}$") {
        $m = ($m.ToUpper() -split "(.{2})" | Where-Object { $_ }) -join ":"
    }
    return $m.ToUpper()
}

function Convert-GuidFormat([string]$g) {
    if ([string]::IsNullOrWhiteSpace($g)) { return $g }
    return $g.Trim().ToUpper()
}
#endregion

try {
    Write-Log "=== START SCCM Create Device + PXE Boot ===" "INFO" "MAIN"

    #region Import SCCM Module + Connect
    Write-Log "Importing ConfigurationManager module..." "INFO" "SCCM"
    Import-Module "C:\Windows\System32\WindowsPowerShell\v1.0\Modules\SCCM\bin\ConfigurationManager.psd1" -Force

    $secpasswd = ConvertTo-SecureString '^[SCCMPW]' -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential("^[SCCMUser]", $secpasswd)

    $initParams = @{}

    $site = '^[SCCMSite]'
    $siteServer = '^[SCCMSiteServer]'

    Write-Log "Ensuring SCCM PSDrive exists (site=$site, server=$siteServer)..." "INFO" "SCCM"
    if ($null -eq (Get-PSDrive -Name $site -PSProvider CMSite -ErrorAction SilentlyContinue)) {
        New-PSDrive -Name $site -PSProvider CMSite -Root $siteServer -Credential $cred @initParams | Out-Null
        Write-Log "Created SCCM PSDrive '$site'." "INFO" "SCCM"
    } else {
        Write-Log "SCCM PSDrive '$site' already exists." "DEBUG" "SCCM"
    }

    Set-Location "$site`:\" @initParams
    Write-Log "Set-Location to SCCM site drive: $site`:\\" "DEBUG" "SCCM"
    #endregion

    #region Read + Validate Inputs
    $VMName         = "$[VMName]".Trim()
    $OSCollectionID = "$[OSCollectionID]".Trim()
    $MACAddress     = Convert-MacAddress "$[MACAddress]"
    $SCCMGuiD       = Convert-GuidFormat "$[SCCMGuiD]"
    $AppCollectionIDsRaw = "$[AppCollectionIDs]"

    Test-NotEmpty $VMName "VMName"
    Test-NotEmpty $OSCollectionID "OSCollectionID"

    $AppCollectionIDsArray = @()
    if (-not [string]::IsNullOrWhiteSpace($AppCollectionIDsRaw)) {
        $AppCollectionIDsArray = $AppCollectionIDsRaw -split ";" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    }

    Write-Log ("Inputs: VMName='{0}', OSCollectionID='{1}', MAC='{2}', GUID='{3}', AppCollections={4}" -f `
        $VMName, $OSCollectionID, $MACAddress, $SCCMGuiD, ($AppCollectionIDsArray.Count)) "INFO" "INPUT"
    #endregion

    #region SAFETY STOP #1: SCCM must have 0 or 1 device with this name
    Write-Log "SAFETY: Checking SCCM devices count for '$VMName'..." "INFO" "SAFETY"
    $devices = @(Get-CMDevice -Name $VMName -ErrorAction SilentlyContinue)

    if ($devices.Count -gt 1) {
        Write-Log "SAFETY STOP: Multiple SCCM devices found for '$VMName' (count=$($devices.Count)). Refusing to proceed." "ERROR" "SAFETY"
        $devices | Select-Object Name, ResourceID | ForEach-Object {
            Write-Log ("Duplicate: Name={0}, ResourceID={1}" -f $_.Name, $_.ResourceID) "ERROR" "SAFETY"
        }
        throw "Safety stop: Multiple SCCM devices for same VMName."
    }

    $ExistingDevice = $devices | Select-Object -First 1
    if ($ExistingDevice) {
        Write-Log "SCCM device exists: ResourceID=$($ExistingDevice.ResourceID)" "INFO" "SCCM"
    } else {
        Write-Log "SCCM device not found." "INFO" "SCCM"
    }
    #endregion

    #region Create Device if missing
    if (-not $ExistingDevice) {
        Write-Log "Creating SCCM device via Import-CMComputerInformation..." "INFO" "SCCM"
        Import-CMComputerInformation `
            -CollectionId $OSCollectionID `
            -ComputerName $VMName `
            -MacAddress $MACAddress `
            -SMBiosGuid $SCCMGuiD `
            -Verbose | Out-Null
        Write-Log "Import submitted." "INFO" "SCCM"
    } else {
        Write-Log "Skipping create: already exists." "INFO" "SCCM"
    }
    #endregion

    #region Retrieve ResourceID with Retry
    Write-Log "Retrieving ResourceID (max 60 attempts, 60s sleep)..." "INFO" "SCCM"
    $ResourceID = $null
    if ($ExistingDevice -and $ExistingDevice.ResourceID) { $ResourceID = $ExistingDevice.ResourceID }

    $RetryCounter = 0
    while (-not $ResourceID -and $RetryCounter -lt 60) {
        $RetryCounter++
        Write-Log "Waiting for ResourceID (attempt $RetryCounter/60)..." "INFO" "SCCM"
        Start-Sleep -Seconds 60

        # SAFETY: still must be <=1 device
        $devicesNow = @(Get-CMDevice -Name $VMName -ErrorAction SilentlyContinue)
        if ($devicesNow.Count -gt 1) {
            Write-Log "SAFETY STOP: Multiple SCCM devices appeared during retry for '$VMName'." "ERROR" "SAFETY"
            throw "Safety stop: Multiple SCCM devices appeared during ResourceID polling."
        }

        $d = $devicesNow | Select-Object -First 1
        if ($d -and $d.ResourceID) {
            $ResourceID = $d.ResourceID
            Write-Log "ResourceID retrieved: $ResourceID" "INFO" "SCCM"
        }
    }

    if (-not $ResourceID) {
        Write-Log "Failed to retrieve ResourceID within 60 minutes. Skipping App collection assignments." "ERROR" "SCCM"
    } else {
        #region Add Device to Application Collections
        if ($AppCollectionIDsArray.Count -eq 0) {
            Write-Log "No AppCollectionIDs provided. Skipping app collection assignments." "INFO" "SCCM"
        } else {
            Write-Log "Assigning device to App Collections (count=$($AppCollectionIDsArray.Count))..." "INFO" "SCCM"
            foreach ($CollectionID in $AppCollectionIDsArray) {
                Invoke-Retry -MaxAttempts 10 -DelaySeconds 3 -Context "SCCM" -ActionName "Add membership to Collection $CollectionID" -ScriptBlock {
                    Add-CMDeviceCollectionDirectMembershipRule -CollectionId $CollectionID -ResourceId $ResourceID -ErrorAction Stop | Out-Null
                } | Out-Null
                Write-Log "Added ResourceID $ResourceID to Collection $CollectionID." "INFO" "SCCM"
            }
        }
        #endregion
    }
    #endregion

    #region Wait + Collection Updates
    Write-Log "Waiting 120s before collection updates..." "INFO" "SCCM"
    Start-Sleep -Seconds 120

    Write-Log "Triggering OS collection update for $OSCollectionID..." "INFO" "SCCM"
    (Get-CMCollection -Id $OSCollectionID) | Invoke-CMCollectionUpdate | Out-Null

    Write-Log "Triggering VDI limiting collection update (INA0E66B)..." "INFO" "SCCM"
    (Get-CMCollection -Id "INA0E66B") | Invoke-CMCollectionUpdate | Out-Null

    Write-Log "Waiting 120s after collection updates..." "INFO" "SCCM"
    Start-Sleep -Seconds 120
    #endregion

    #region SAFETY STOP #2: OS collection must have max 1 member
    Write-Log "SAFETY: Checking OS collection member count for '$OSCollectionID'..." "INFO" "SAFETY"
    try {
        $members = @(Get-CMCollectionMember -CollectionId $OSCollectionID -ErrorAction Stop)
        Write-Log "OS collection members count: $($members.Count)" "INFO" "SAFETY"

        if ($members.Count -gt 1) {
            Write-Log "SAFETY STOP: OS collection '$OSCollectionID' contains $($members.Count) members. Refusing to PXE-boot." "ERROR" "SAFETY"
            $members | Select-Object Name, ResourceID | ForEach-Object {
                Write-Log ("Member: Name={0}, ResourceID={1}" -f $_.Name, $_.ResourceID) "ERROR" "SAFETY"
            }
            throw "Safety stop: OS collection has more than 1 member."
        }
    }
    catch {
        Write-Log "Could not evaluate OS collection member count (Get-CMCollectionMember failed): $($_.Exception.Message)" "WARN" "SAFETY"
        Write-Log "If this cmdlet is unavailable in your environment, tell me and I will adapt this guard to your SCCM version." "WARN" "SAFETY"
        throw
    }
    #endregion

    #region vSphere Connect
    Write-Log "Connecting to vSphere server..." "INFO" "VSPHERE"
    $secpasswd = ConvertTo-SecureString '^[vSphereServerAdminPW]' -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential("^[vSphereServerAdminUser]", $secpasswd)

    Connect-VIServer -Server "^[vSphereServerHost]" -Credential $cred | Out-Null
    Write-Log "Connected to vSphere." "INFO" "VSPHERE"
    #endregion

    #region SAFETY STOP #3: vSphere must have exactly 1 VM with this name
    Write-Log "SAFETY: Checking vSphere VM count for '$VMName'..." "INFO" "SAFETY"
    $vms = @(Get-VM -Name $VMName -ErrorAction Stop)
    if ($vms.Count -ne 1) {
        Write-Log "SAFETY STOP: Expected exactly 1 VM named '$VMName', but found $($vms.Count)." "ERROR" "SAFETY"
        throw "Safety stop: vSphere VM name not unique."
    }
    $VM = $vms[0]
    Write-Log "vSphere VM resolved uniquely: $($VM.Name) (PowerState=$($VM.PowerState))" "INFO" "SAFETY"
    #endregion

    #region Retrieve Deployment Details
    Write-Log "Retrieving deployment details..." "INFO" "SCCM"
    $OSCollectionName = (Get-CMDeviceCollection -Id $OSCollectionID).Name
    Test-NotEmpty $OSCollectionName "OSCollectionName"
    Write-Log "OS Collection Name: $OSCollectionName" "INFO" "SCCM"

    $deployment = Get-CMDeployment -CollectionName $OSCollectionName | Select-Object -First 1
    if (-not $deployment) { throw "No deployment found for collection '$OSCollectionName'." }

    $DeploymentID = $deployment.DeploymentID
    Test-NotEmpty $DeploymentID "DeploymentID"
    Write-Log "DeploymentID: $DeploymentID" "INFO" "SCCM"

    $DeploymentStatus = Get-CMDeploymentStatus -DeploymentId $DeploymentID |
        Get-CMDeploymentStatusDetails |
        Where-Object DeviceName -eq $VMName |
        Select-Object -First 1

    if ($DeploymentStatus) {
        Write-Log "Initial DeploymentStatus: StatusType=$($DeploymentStatus.StatusType), Desc=$($DeploymentStatus.StatusDescription)" "INFO" "SCCM"
    } else {
        Write-Log "No DeploymentStatusDetails found yet for device '$VMName'." "WARN" "SCCM"
    }
    #endregion

    #region PXE Boot Process (single VM only)
    $PXEAttempts = 1
    while ((-not $DeploymentStatus) -or ($DeploymentStatus.StatusType -ne "2")) {
        if ($PXEAttempts -gt 2) { break }

        Write-Log "Attempting PXE Boot (attempt $PXEAttempts/2)..." "INFO" "PXE"

        # Always operate only on the single resolved VM object
        if ($VM.PowerState -eq 'PoweredOn') {
            Write-Log "Stopping VM (no confirm)..." "INFO" "PXE"
            Stop-VM -VM $VM -Confirm:$false | Out-Null

            do {
                Start-Sleep -Seconds 5
                $VM = (Get-VM -Name $VMName | Select-Object -First 1)
            } while ($VM.PowerState -eq 'PoweredOn')

            Write-Log "VM is now powered off." "INFO" "PXE"
        }

        Write-Log "Starting VM..." "INFO" "PXE"
        Start-VM -VM $VM | Out-Null

        Write-Log "VM started. Waiting 600s for PXE/TS to begin..." "INFO" "PXE"
        Start-Sleep -Seconds 600

        $DeploymentStatus = Get-CMDeploymentStatus -DeploymentId $DeploymentID |
            Get-CMDeploymentStatusDetails |
            Where-Object DeviceName -eq $VMName |
            Select-Object -First 1

        if ($DeploymentStatus) {
            Write-Log ("DeploymentStatus after PXE attempt {0}: StatusType={1}, Desc={2}" -f $PXEAttempts, $DeploymentStatus.StatusType, $DeploymentStatus.StatusDescription) "INFO" "PXE"
        } else {
            Write-Log ("Still no DeploymentStatusDetails after PXE attempt {0}." -f $PXEAttempts) "WARN" "PXE"
        }

        $PXEAttempts++
    }
    #endregion

    #region Final Result
    if (-not $DeploymentStatus -or $DeploymentStatus.StatusType -ne "2") {
        $last = if ($DeploymentStatus) { $DeploymentStatus.StatusDescription } else { "No status available" }
        Write-Log "Task sequence failed to start after multiple attempts. Last status: $last" "ERROR" "RESULT"
        $Global:TaskSequenceResult = "TaskSeqStartError"
    } else {
        Write-Log "Task sequence successfully started: $($DeploymentStatus.StatusDescription)" "INFO" "RESULT"
        $Global:TaskSequenceResult = "TaskSeqStartSuccess"
    }

    Write-Log "=== END (SUCCESS PATH) ===" "INFO" "MAIN"
    #endregion
}
catch {
    Write-Log "Unhandled error: $($_.Exception.Message)" "ERROR" "MAIN"
    Write-Log "Stack: $($_.ScriptStackTrace)" "DEBUG" "MAIN"
    $Global:TaskSequenceResult = "ScriptUnhandledError"
    throw
}
finally {
    try {
        if (Get-Command Disconnect-VIServer -ErrorAction SilentlyContinue) {
            Disconnect-VIServer -Server * -Confirm:$false | Out-Null
            Write-Log "Disconnected from vSphere." "DEBUG" "VSPHERE"
        }
    } catch {
        Write-Log "Disconnect-VIServer failed (ignored): $($_.Exception.Message)" "DEBUG" "VSPHERE"
    }
}
