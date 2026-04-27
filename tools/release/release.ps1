<#
.SYNOPSIS
  PowerShell wrapper that invokes the canonical release.sh under Git for
  Windows' bash. Avoids the ``bash`` → WSL hijack on Windows.

.DESCRIPTION
  ``bash tools\release\release.sh 0.5.0`` from PowerShell routes through
  ``C:\Windows\System32\bash.exe``, which is the WSL launcher. When WSL
  has no Linux distro installed (or just isn't the bash you wanted) it
  fails with ``execvpe(/bin/bash) failed: No such file or directory``.

  This wrapper locates Git for Windows' bash.exe and forwards arguments
  verbatim to release.sh. The .sh stays the single source of truth — no
  PowerShell port to drift from it.

.EXAMPLE
  .\tools\release\release.ps1 0.5.0
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string]$NewVersion,

  # Anything after the version is forwarded to release.sh as-is so future
  # flags (--write, --verbose, …) work without editing this wrapper.
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Forward
)

$ErrorActionPreference = 'Stop'

function Find-GitBash {
  # 1) Derive from git.exe's location — works for any Git for Windows
  #    install regardless of drive / path. ``git.exe`` lives under
  #    ``<install>\cmd\``; ``bash.exe`` under ``<install>\bin\``.
  $gitCmd = Get-Command git -CommandType Application -ErrorAction SilentlyContinue
  if ($gitCmd) {
    $gitInstall = Split-Path -Parent (Split-Path -Parent $gitCmd.Source)
    $candidate = Join-Path $gitInstall 'bin\bash.exe'
    if (Test-Path $candidate) { return $candidate }
  }
  # 2) Fall back to the standard install paths.
  foreach ($p in @(
      'C:\Program Files\Git\bin\bash.exe',
      'C:\Program Files (x86)\Git\bin\bash.exe'
    )) {
    if (Test-Path $p) { return $p }
  }
  return $null
}

$bash = Find-GitBash
if (-not $bash) {
  Write-Error @"
Could not locate Git for Windows' bash.exe.
This wrapper needs it to run release.sh without going through WSL.

Either install Git for Windows (https://git-scm.com/download/win),
or open a Git Bash terminal directly and run:

    bash tools/release/release.sh $NewVersion
"@
  exit 1
}

# release.sh sits next to this wrapper.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$releaseSh = Join-Path $scriptDir 'release.sh'
if (-not (Test-Path $releaseSh)) {
  Write-Error "release.sh not found alongside this wrapper (expected at $releaseSh)."
  exit 1
}

# Hand off. ``-LiteralPath``-style invocation isn't available for native
# exes, but Git Bash handles Windows-style paths fine.
& $bash $releaseSh $NewVersion @Forward
exit $LASTEXITCODE
