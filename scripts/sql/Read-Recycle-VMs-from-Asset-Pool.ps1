param(
    [int]$DaysInStatus = 1
)

# Read Recycle VMs from Asset Pool
# Selects at most ONE asset that has been in status 'Reinstall' for at least
# $DaysInStatus days (based on last_reclaim_at, which is set by release_asset
# immediately before mark_reinstall — see worker/tasks/modules/pool_manager.py).
#
# Exports for subsequent steps:
#   $global:RecycleVM               – full row object (id, name, asset_type_id, last_reclaim_at)
#   $global:RecycleVmId             – numeric id
#   $global:RecycleVmName           – VM name
#   $global:RecycleVmAssetTypeId    – asset type id
#
# Uses db_query.py helper (psycopg2 + DATABASE_URL, no credentials needed).

$sql = @"
SELECT id, name, asset_type_id, last_reclaim_at
FROM asset_pool
WHERE status::text = %s
  AND (last_reclaim_at IS NULL
       OR last_reclaim_at <= NOW() - make_interval(days => %s::int))
ORDER BY last_reclaim_at ASC NULLS FIRST
LIMIT 1
"@

$json = python /app/tasks/utils/db_query.py $sql "Reinstall" "$DaysInStatus"
$parsed = @($json | ConvertFrom-Json)

# Error from db_query.py (single-object array with 'error' key)
if ($parsed.Count -eq 1 -and $parsed[0].PSObject.Properties.Name -contains "error") {
    Write-Output (@{ success = $false; error = $parsed[0].error } | ConvertTo-Json -Compress)
    exit 1
}

if ($parsed.Count -eq 0) {
    $global:RecycleVM = $null
    $global:RecycleVmId = $null
    $global:RecycleVmName = $null
    $global:RecycleVmAssetTypeId = $null
    Write-Output (@{
        success        = $true
        count          = 0
        days_in_status = $DaysInStatus
        message        = "No assets eligible (minimum $DaysInStatus day(s) in status 'Reinstall')."
    } | ConvertTo-Json -Compress)
    exit 0
}

$vm = $parsed[0]
$global:RecycleVM = $vm
$global:RecycleVmId = $vm.id
$global:RecycleVmName = $vm.name
$global:RecycleVmAssetTypeId = $vm.asset_type_id

Write-Output (@{
    success         = $true
    count           = 1
    id              = $vm.id
    name            = $vm.name
    asset_type_id   = $vm.asset_type_id
    last_reclaim_at = $vm.last_reclaim_at
    days_in_status  = $DaysInStatus
} | ConvertTo-Json -Compress)
