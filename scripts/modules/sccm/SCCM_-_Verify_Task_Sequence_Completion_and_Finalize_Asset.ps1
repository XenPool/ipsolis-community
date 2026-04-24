# NAME: SCCM - Verify Task Sequence Completion and Finalize Asset
# DESC: Finalises the recycle runbook: polls SCCM until the post-OSD ConfigMgr client registers (or a failure is detected) and sets asset_pool.status to 'free' or 'Failed' accordingly.
param(
    # Not Mandatory: when the runner stops early (RunbookStopped), this step
    # still runs (always_run=true) but has no VM to work on. The script body
    # detects the stop signal and short-circuits as a no-op.
    [string]$VMName = "",
    [string]$SCCMResourceID = "",
    [int]$CompletionWaitSeconds = 5400,   # 90 min default
    [int]$CompletionCheckSec    = 60
)

# SCCM - Verify Task Sequence Completion and Finalize Asset Status
#
# Runs as the final (always_run) step of the recycle runbook:
#   * If the runbook has already accumulated a failure (RunbookFailed global
#     injected by standalone_runner), skip the SCCM check and flip the asset
#     straight to 'Failed'.
#   * Otherwise, poll SCCM for evidence the OSD task sequence actually
#     completed on the new device (SMS_R_System.Client = 1 for the imported
#     ResourceID, which turns true once the post-OSD ConfigMgr client has
#     registered with the MP).
#   * Set asset_pool.status to 'Free' on success, 'Failed' on TS failure or
#     timeout.

$ErrorActionPreference = 'Stop'

function Write-Log {
    param([string]$Message, [ValidateSet('INFO','WARNING','ERROR','SUCCESS')][string]$Level='INFO')
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Write-Host "[$ts] [$Level] $Message"
}

function Update-AssetStatus([string]$name, [string]$status) {
    $sql = "UPDATE asset_pool SET status = %s::asset_status, updated_at = NOW() WHERE name = %s"
    $raw = python /app/tasks/utils/db_execute.py $sql $status $name 2>&1
    $text = if ($raw -is [array]) { $raw -join "`n" } else { [string]$raw }
    try { $res = $text | ConvertFrom-Json } catch {
        throw "db_execute.py non-JSON: $text"
    }
    if (-not $res.success) { throw "asset_pool update failed: $($res.error)" }
    Write-Log "asset_pool.status for '$name' -> '$status' ($($res.rowcount) row(s))" 'SUCCESS'
    return [int]$res.rowcount
}

# --- SCCM helpers (same pattern as Import/Delete scripts) ------------------
function Get-SccmConfig {
    $json = python /app/tasks/utils/db_query.py `
        "SELECT key, value FROM app_config WHERE key LIKE %s" "sccm.%"
    $rows = $json | ConvertFrom-Json
    $cfg = @{}
    foreach ($r in $rows) { $cfg[$r.key.Substring(5)] = $r.value }
    foreach ($k in 'base_url','username','password','realm','kdc') {
        if ([string]::IsNullOrWhiteSpace($cfg[$k])) { throw "app_config key 'sccm.$k' is empty" }
    }
    if (-not $cfg.ContainsKey('verify_tls')) { $cfg['verify_tls'] = 'true' }
    return $cfg
}

function Invoke-Kinit([hashtable]$cfg) {
    $krb5conf = "[libdefaults]`n    default_realm = $($cfg.realm)`n    dns_lookup_kdc = false`n    dns_lookup_realm = false`n[realms]`n    $($cfg.realm) = {`n        kdc = $($cfg.kdc)`n        admin_server = $($cfg.kdc)`n    }`n"
    $krbPath = "/tmp/krb5_xp_$PID.conf"
    [IO.File]::WriteAllText($krbPath, $krb5conf)
    $env:KRB5_CONFIG = $krbPath
    $env:KRB5CCNAME  = "/tmp/krb5cc_xp_$PID"
    $principal = $cfg.username
    if ($principal -match '\\') { $principal = ($principal -split '\\')[-1] }
    if ($principal -notmatch '@') { $principal = "$principal@$($cfg.realm)" }
    $pwFile = "/tmp/kinit_pw_$PID"
    [IO.File]::WriteAllText($pwFile, $cfg.password)
    try {
        $out = bash -c "kinit -V '$principal' < '$pwFile' 2>&1"
        if ($LASTEXITCODE -ne 0) { throw "kinit failed ($LASTEXITCODE): $out" }
    } finally { Remove-Item $pwFile -Force -ErrorAction SilentlyContinue }
}
function Clear-Kinit { & kdestroy 2>&1 | Out-Null }

function Get-SccmUrl([hashtable]$cfg, [string]$path) {
    $root = $cfg.base_url.TrimEnd('/')
    if (-not $root.ToLower().EndsWith('/adminservice')) { $root += '/AdminService' }
    return "$root/$path"
}

function Invoke-SccmRequest([hashtable]$cfg, [string]$method, [string]$path, [hashtable]$query = $null) {
    $url = Get-SccmUrl $cfg $path
    if ($query) {
        $qs = @()
        foreach ($k in $query.Keys) { $qs += "$k=" + [uri]::EscapeDataString([string]$query[$k]) }
        if ($qs.Count -gt 0) { $url += '?' + ($qs -join '&') }
    }
    $curlArgs = @('-sS', '-w', '\nHTTP_STATUS:%{http_code}', '-X', $method.ToUpper(),
                  '--negotiate', '-u', ':', '-H', 'Accept: application/json')
    if ($cfg.verify_tls -eq 'false') { $curlArgs = @('-k') + $curlArgs }
    $curlArgs += $url
    $raw = & /usr/bin/curl @curlArgs 2>&1
    $exit = $LASTEXITCODE
    $text = if ($raw -is [array]) { $raw -join "`n" } else { [string]$raw }
    $status = ''; $respBody = $text
    if ($text -match '(?s)(.*)\nHTTP_STATUS:(\d+)\s*$') { $respBody = $Matches[1]; $status = $Matches[2] }
    if ($exit -ne 0 -or $status -eq '' -or [int]$status -ge 400) {
        $snip = $respBody.Trim(); if ($snip.Length -gt 500) { $snip = $snip.Substring(0,500) }
        throw "$method $url failed (curl=$exit http=$status): $snip"
    }
    if ([string]::IsNullOrWhiteSpace($respBody)) { return $null }
    try { return $respBody | ConvertFrom-Json } catch { return $respBody }
}

# --- main -------------------------------------------------------------------
try {
    # Runner exposes RunbookStopped when a prior step emitted stop_run=true
    # (e.g. "Read Recycle VMs" found zero eligible assets). There is nothing
    # to finalize in that case — exit as a no-op success so the run ends clean.
    $runbookStopped = $false
    try {
        if ($null -ne (Get-Variable -Name 'RunbookStopped' -Scope Global -ErrorAction SilentlyContinue)) {
            $v = (Get-Variable -Name 'RunbookStopped' -Scope Global).Value
            if ($v -is [bool]) { $runbookStopped = $v }
            elseif ($v) { $runbookStopped = [string]$v -match '^(?i:true|1|yes)$' }
        }
    } catch {}

    # No VM to finalize (either the runner signalled an early stop, or the
    # upstream "Read Recycle VMs" step found nothing eligible). In both
    # cases there is literally no asset to touch — exit cleanly as success
    # so the overall run ends green.
    if ($runbookStopped -or [string]::IsNullOrWhiteSpace($VMName)) {
        $reason = if ($runbookStopped) {
            'Runbook stopped early; no asset to finalize.'
        } else {
            'No VMName supplied; no asset to finalize.'
        }
        Write-Log $reason 'INFO'
        Write-Output (@{
            success      = $true
            skipped      = $true
            asset_status = 'unchanged'
            reason       = $reason
        } | ConvertTo-Json -Compress)
        exit 0
    }

    # Runner exposes RunbookFailed as a step_var / global when a prior
    # critical step failed. In that case we do not touch SCCM - the new
    # device may not even exist - we just mark the asset 'Failed'.
    $priorFailure = $false
    try {
        if ($null -ne (Get-Variable -Name 'RunbookFailed' -Scope Global -ErrorAction SilentlyContinue)) {
            $v = (Get-Variable -Name 'RunbookFailed' -Scope Global).Value
            if ($v -is [bool]) { $priorFailure = $v }
            elseif ($v) { $priorFailure = [string]$v -match '^(?i:true|1|yes)$' }
        }
    } catch {}

    if ($priorFailure) {
        $failedStep = ''
        try { $failedStep = (Get-Variable -Name 'RunbookFirstFailedStep' -Scope Global -ErrorAction SilentlyContinue).Value } catch {}
        Write-Log "Prior failure detected (first failed step: '$failedStep'). Marking asset 'Failed'." 'WARNING'
        Update-AssetStatus $VMName 'Failed' | Out-Null
        Write-Output (@{
            success      = $false
            asset_status = 'Failed'
            reason       = "Earlier runbook step failed: $failedStep"
        } | ConvertTo-Json -Compress)
        exit 1
    }

    # Happy path: verify task sequence actually completed by waiting for the
    # post-OSD client to register. SMS_R_System.Client flips to 1 once the
    # reinstalled OS has booted and the ConfigMgr client has contacted the MP.
    $cfg = Get-SccmConfig
    Invoke-Kinit $cfg

    $safeName = $VMName -replace "'", "''"
    $ridFilter = $null
    if (-not [string]::IsNullOrWhiteSpace($SCCMResourceID)) {
        try { $ridFilter = [int]$SCCMResourceID } catch { $ridFilter = $null }
    }

    Write-Log "Polling SCCM for TS completion (timeout $CompletionWaitSeconds s, interval $CompletionCheckSec s)..." 'INFO'
    $completed = $false
    $completionReason = ''
    $lastClient = $null
    $elapsed = 0
    # Track wall-clock we started polling so we can filter TS status messages
    # from *this* run (avoids false-positives from prior OSD cycles).
    $pollStartIso = (Get-Date).ToUniversalTime().AddMinutes(-20).ToString("yyyy-MM-ddTHH:mm:ssZ")
    while ($elapsed -lt $CompletionWaitSeconds) {
        # --- Signal A: any SMS_R_System row for this Name has Client=1 -----
        # After OSD, SCCM may create a *new* ResourceID for the re-imaged
        # device while the pre-OSD import row stays at Client=0 forever.
        # So we query by Name and succeed on ANY matching row with Client=1.
        try {
            $resp = Invoke-SccmRequest $cfg 'Get' 'wmi/SMS_R_System' @{
                '$filter' = "Name eq '$safeName'"
                '$select' = 'ResourceID,Name,Client,ClientType,AgentTime,LastLogonTimestamp'
            }
            $devices = @($resp.value)
            if ($devices.Count -ge 1) {
                $snap = ($devices | ForEach-Object {
                    "RID=$($_.ResourceID)/Client=$($_.Client)"
                }) -join ', '
                Write-Log "SMS_R_System snapshot: $snap" 'INFO'
                $active = @($devices | Where-Object { [int]$_.Client -eq 1 })
                if ($active.Count -ge 1) {
                    $lastClient = 1
                    $completed = $true
                    $completionReason = "Client=1 on ResourceID $($active[0].ResourceID)"
                    break
                }
                $lastClient = [int]($devices[0].Client)
            } else {
                Write-Log "Device not (yet) present in SMS_R_System." 'WARNING'
            }
        } catch {
            Write-Log "SMS_R_System query error: $($_.Exception.Message)" 'WARNING'
        }

        # --- Signal B: TS completion / OSD-finished status message --------
        # MessageIDs: 11171 = TS completed successfully, 11143 = TS action
        # "Setup Windows and ConfigMgr" finished, 10005 = ConfigMgr client
        # Health Evaluation OK post-OSD. Any one is sufficient evidence.
        try {
            $tsFilter = "MachineName eq '$safeName' and MessageID eq 11171 and Time gt $pollStartIso"
            $tsResp = Invoke-SccmRequest $cfg 'Get' 'wmi/SMS_StatMsgWithInsStrings' @{
                '$filter'  = $tsFilter
                '$select'  = 'RecordID,MachineName,MessageID,Time'
                '$orderby' = 'Time desc'
                '$top'     = '1'
            }
            $tsMsgs = @($tsResp.value)
            if ($tsMsgs.Count -ge 1) {
                Write-Log "TS success status message found: MessageID=$($tsMsgs[0].MessageID) at $($tsMsgs[0].Time)" 'SUCCESS'
                $completed = $true
                $completionReason = "MessageID 11171 (TS succeeded) at $($tsMsgs[0].Time)"
                break
            }
        } catch {
            Write-Log "Status message query error: $($_.Exception.Message)" 'WARNING'
        }

        Write-Log "  [ts-completion poll $elapsed/$CompletionWaitSeconds s] No completion signal yet." 'INFO'
        Start-Sleep -Seconds $CompletionCheckSec
        $elapsed += $CompletionCheckSec
    }

    if ($completed) {
        Update-AssetStatus $VMName 'Free' | Out-Null
        Write-Output (@{
            success      = $true
            asset_status = 'Free'
            resource_id  = $ridFilter
            reason       = $completionReason
            message      = "Task sequence completed; $completionReason."
        } | ConvertTo-Json -Compress)
        exit 0
    } else {
        Update-AssetStatus $VMName 'Failed' | Out-Null
        Write-Output (@{
            success      = $false
            asset_status = 'Failed'
            resource_id  = $ridFilter
            error        = "Task sequence did not complete within $CompletionWaitSeconds s (Client flag last seen: $lastClient)."
        } | ConvertTo-Json -Compress)
        exit 1
    }
}
catch {
    # On unexpected errors, still try to mark 'Failed' so the asset doesn't
    # stay wedged in 'Reinstalling'.
    try { Update-AssetStatus $VMName 'Failed' | Out-Null } catch {}
    Write-Output (@{ success = $false; asset_status = 'Failed'; error = $_.Exception.Message } | ConvertTo-Json -Compress)
    exit 1
}
finally {
    Clear-Kinit
}
