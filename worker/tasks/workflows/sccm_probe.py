"""Celery task: SCCM Admin Service connection probe (runs in worker).

The API container does not ship pwsh or krb5; this task runs a tiny pwsh script
inside the worker to exercise the exact same path the SCCM modules use:
    kinit → Invoke-RestMethod -Authentication Negotiate -UseDefaultCredentials

Invoked synchronously from the API (`.get(timeout=...)`).
Returns `{ok: bool, message: str}`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import psycopg2
import psycopg2.extras

from tasks import app


PROBE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $cfg = @{
        base_url   = $env:SCCM_BASE_URL
        username   = $env:SCCM_USERNAME
        password   = $env:SCCM_PASSWORD
        realm      = $env:SCCM_REALM
        kdc        = $env:SCCM_KDC
        verify_tls = $env:SCCM_VERIFY_TLS
    }
    foreach ($k in 'base_url','username','password','realm','kdc') {
        if ([string]::IsNullOrWhiteSpace($cfg[$k])) {
            Write-Output (@{ ok = $false; message = "Missing sccm.$k"} | ConvertTo-Json -Compress)
            exit 0
        }
    }

    # krb5.conf
    $krb5 = "[libdefaults]`n    default_realm = $($cfg.realm)`n    dns_lookup_kdc = false`n    dns_lookup_realm = false`n[realms]`n    $($cfg.realm) = {`n        kdc = $($cfg.kdc)`n        admin_server = $($cfg.kdc)`n    }`n"
    $krbPath = "/tmp/krb5_probe_$PID.conf"
    [IO.File]::WriteAllText($krbPath, $krb5)
    $env:KRB5_CONFIG = $krbPath
    $env:KRB5CCNAME  = "/tmp/krb5cc_probe_$PID"

    # Kerberos principals are user@REALM. Strip any NT-style DOMAIN\ prefix
    # (e.g. 'XENPOOL\Administrator' -> 'Administrator').
    $principal = $cfg.username
    if ($principal -match '\\') { $principal = ($principal -split '\\')[-1] }
    if ($principal -notmatch '@') { $principal = "$principal@$($cfg.realm)" }

    $pwFile = "/tmp/kinit_pw_probe_$PID"
    [IO.File]::WriteAllText($pwFile, $cfg.password)
    try {
        $out = bash -c "kinit -V '$principal' < '$pwFile' 2>&1"
        $code = $LASTEXITCODE
    } finally {
        Remove-Item $pwFile -Force -ErrorAction SilentlyContinue
    }
    if ($code -ne 0) {
        Write-Output (@{ ok = $false; message = "kinit failed ($code): $out" } | ConvertTo-Json -Compress)
        exit 0
    }

    $root = $cfg.base_url.TrimEnd('/')
    if (-not $root.ToLower().EndsWith('/adminservice')) { $root += '/AdminService' }
    $url = "$root/wmi/SMS_Site?`$top=1"

    # Linux pwsh's Invoke-RestMethod lacks Kerberos Negotiate support, so delegate to
    # curl --negotiate which consumes the TGT from KRB5CCNAME.
    $curlArgs = @('-sS', '-w', '\nHTTP_STATUS:%{http_code}', '--negotiate', '-u', ':',
                  '-H', 'Accept: application/json', $url)
    if ($cfg.verify_tls -eq 'false') { $curlArgs = @('-k') + $curlArgs }

    $raw = & /usr/bin/curl @curlArgs 2>&1
    $curlExit = $LASTEXITCODE
    $text = if ($raw -is [array]) { $raw -join "`n" } else { [string]$raw }
    $status = ''
    $body = $text
    if ($text -match '(?s)(.*)\nHTTP_STATUS:(\d+)\s*$') {
        $body = $Matches[1]
        $status = $Matches[2]
    }

    if ($curlExit -ne 0 -or $status -eq '' -or [int]$status -ge 400) {
        $snip = $body.Trim(); if ($snip.Length -gt 300) { $snip = $snip.Substring(0,300) }
        Write-Output (@{ ok = $false; message = "GET $url failed (curl=$curlExit http=$status): $snip" } | ConvertTo-Json -Compress)
        & kdestroy 2>&1 | Out-Null
        exit 0
    }

    try { $resp = $body | ConvertFrom-Json } catch {
        Write-Output (@{ ok = $false; message = "Invalid JSON from Admin Service: $($body.Substring(0,[Math]::Min(200,$body.Length)))" } | ConvertTo-Json -Compress)
        & kdestroy 2>&1 | Out-Null
        exit 0
    }

    $rows = @($resp.value)
    $siteCode = if ($rows.Count -gt 0 -and $rows[0].SiteCode) { $rows[0].SiteCode } else { '' }
    $msg = "Admin Service reachable via Kerberos - SMS_Site returned $($rows.Count) row(s)"
    if ($siteCode) { $msg += " (site code: $siteCode)" }
    Write-Output (@{ ok = $true; message = ($msg + '.') } | ConvertTo-Json -Compress)

    & kdestroy 2>&1 | Out-Null
}
catch {
    Write-Output (@{ ok = $false; message = $_.Exception.Message } | ConvertTo-Json -Compress)
}
"""


def _load_sccm_config() -> dict:
    db_url = os.environ.get("DATABASE_URL", "")
    dsn = db_url.split("+")[0] + "://" + db_url.split("://", 1)[1] if "+" in db_url else db_url
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT key, value FROM app_config WHERE key LIKE 'sccm.%%'")
        rows = cur.fetchall()
    finally:
        conn.close()
    return {r["key"].split(".", 1)[1]: (r["value"] or "") for r in rows}


@app.task(name="tasks.workflows.sccm_probe.probe")
def probe() -> dict:
    """Runs the pwsh probe and returns {ok, message}."""
    import json

    from tasks.utils.license import is_feature_enabled
    if not is_feature_enabled("sccm_integration"):
        return {"ok": False, "message": "SCCM Integration requires an ip·Solis Enterprise license."}

    cfg = _load_sccm_config()

    env = os.environ.copy()
    env["SCCM_BASE_URL"]   = cfg.get("base_url", "")
    env["SCCM_USERNAME"]   = cfg.get("username", "")
    env["SCCM_PASSWORD"]   = cfg.get("password", "")
    env["SCCM_REALM"]      = cfg.get("realm", "")
    env["SCCM_KDC"]        = cfg.get("kdc", "")
    env["SCCM_VERIFY_TLS"] = cfg.get("verify_tls", "true")

    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
        fh.write(PROBE_SCRIPT)
        script_path = fh.name

    try:
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", script_path],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "pwsh probe timed out after 30s."}
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

    stdout = (result.stdout or "").strip()
    if not stdout:
        err = (result.stderr or "").strip() or "no output"
        return {"ok": False, "message": f"pwsh returned no JSON (exit {result.returncode}): {err[:300]}"}

    # Probe prints a single JSON line; take the last non-empty line to be safe.
    last_line = [ln for ln in stdout.splitlines() if ln.strip()][-1]
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        return {"ok": False, "message": f"Invalid JSON from probe: {last_line[:300]}"}
