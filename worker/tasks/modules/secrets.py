"""Worker-side mirror of ``app.utils.secrets`` (sync only).

The worker package intentionally doesn't import from the API package
(``audit_helper.py`` follows the same boundary). This module duplicates
the small sync resolver so worker code reading credentials from
``app_config`` can dereference ``vault://`` / ``ccp://`` references the
same way the API does.

Reference grammar matches ``app.utils.secrets`` exactly:

* ``vault://<path>[#<field>]`` — KV v2 lookup against the configured mount.
* ``ccp://[<safe>/]<object>`` — CyberArk CCP/AIM lookup.
* ``azurekv://<vault>/<secret>[?version=<v>]`` — Azure Key Vault.
* ``awssm://<secret-id>[#<field>]`` — AWS Secrets Manager (SigV4).
* ``conjur://<identifier>[#<field>]`` — CyberArk Conjur (host API-key auth).

Plain strings pass through unchanged. Resolution failures are logged
and return the empty string — the worker never raises on a backend
outage; the credential just becomes invalid and the calling task
fails with the underlying-system's auth error, which is a clearer
signal than "ipSolis blew up".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10
_KNOWN_SCHEMES = ("vault://", "ccp://", "azurekv://", "awssm://", "conjur://")
_cache: dict[str, tuple[float, str]] = {}
# AAD token cache, separate from the value cache because tokens have
# their own (Azure-driven) expiry. Keyed by (tenant_id, client_id) so
# a config drift can't accidentally mint a token under the wrong tenant.
_aad_token_cache: dict[str, tuple[float, str]] = {}
# Conjur access-token cache. Tokens default to 8 minutes server-side;
# we cache for 7 to leave a 1-minute clock-skew safety margin.
_CONJUR_TOKEN_TTL_SECONDS = 7 * 60
_conjur_token_cache: dict[str, tuple[float, str]] = {}


def is_secret_reference(value: str | None) -> bool:
    return isinstance(value, str) and any(value.startswith(s) for s in _KNOWN_SCHEMES)


def resolve_secret_value(db: Session, raw_value: str | None) -> str:
    """Resolve a possibly-reference value. Sync — caller passes a live Session."""
    if not raw_value or not isinstance(raw_value, str):
        return raw_value or ""
    if not is_secret_reference(raw_value):
        return raw_value

    cached = _cache_get(raw_value)
    if cached is not None:
        return cached

    cfg = _load_secret_cfg(db)
    return _dispatch(raw_value, cfg)


def get_secret_config(db: Session, key: str, default: str = "") -> str:
    """Convenience: ``get_config`` + ``resolve_secret_value`` in one call.

    Worker code that reads a credential row (vsphere.password, etc.)
    should funnel through here instead of ``get_config`` directly so
    upgraded installs get external-secret support without touching
    every call site.
    """
    row = db.execute(
        text("SELECT value FROM app_config WHERE key = :key"),
        {"key": key},
    ).fetchone()
    raw = row[0] if row and row[0] else default
    return resolve_secret_value(db, raw)


# ── Internals ────────────────────────────────────────────────────────────────

def _load_secret_cfg(db: Session) -> dict[str, str]:
    rows = db.execute(
        text("SELECT key, value FROM app_config WHERE key LIKE 'secret.%%'")
    ).fetchall()
    return {row[0][len("secret."):]: (row[1] or "") for row in rows}


def _cache_get(key: str) -> str | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if expires_at < time.time():
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: str, *, ttl_seconds: int) -> None:
    _cache[key] = (time.time() + max(1, ttl_seconds), value)


def _dispatch(raw_value: str, cfg: dict[str, str]) -> str:
    try:
        if raw_value.startswith("vault://"):
            value = _resolve_vault(raw_value[len("vault://"):], cfg)
        elif raw_value.startswith("ccp://"):
            value = _resolve_ccp(raw_value[len("ccp://"):], cfg)
        elif raw_value.startswith("azurekv://"):
            value = _resolve_azurekv(raw_value[len("azurekv://"):], cfg)
        elif raw_value.startswith("awssm://"):
            value = _resolve_awssm(raw_value[len("awssm://"):], cfg)
        elif raw_value.startswith("conjur://"):
            value = _resolve_conjur(raw_value[len("conjur://"):], cfg)
        else:
            return raw_value
    except Exception as exc:  # noqa: BLE001
        logger.warning("secrets: resolution failed for %r: %s", raw_value, exc)
        return ""
    try:
        ttl = int(cfg.get("cache_ttl_seconds", "60") or "60")
    except ValueError:
        ttl = 60
    _cache_set(raw_value, value, ttl_seconds=ttl)
    return value


def _resolve_vault(path: str, cfg: dict[str, str]) -> str:
    if "#" in path:
        path, field = path.split("#", 1)
    else:
        field = "value"
    path = path.strip().strip("/")
    if not path:
        raise ValueError("empty vault path")

    base = (cfg.get("vault.url") or "").strip().rstrip("/")
    token = (cfg.get("vault.token") or "").strip()
    mount = (cfg.get("vault.kv_mount") or "secret").strip().strip("/") or "secret"
    namespace = (cfg.get("vault.namespace") or "").strip()
    if not base or not token:
        raise ValueError("vault.url or vault.token is empty")

    url = f"{base}/v1/{mount}/data/{path}"
    headers = {"X-Vault-Token": token, "Accept": "application/json"}
    if namespace:
        headers["X-Vault-Namespace"] = namespace

    body = _http_get_json(url, headers=headers)
    inner = (((body or {}).get("data") or {}).get("data") or {})
    if field not in inner:
        raise KeyError(f"vault: field {field!r} not present at {path!r}")
    value = inner[field]
    if not isinstance(value, str):
        raise TypeError(f"vault: field {field!r} at {path!r} is not a string")
    return value


def _resolve_ccp(reference: str, cfg: dict[str, str]) -> str:
    if "/" in reference:
        safe, obj = reference.split("/", 1)
    else:
        safe = (cfg.get("ccp.safe") or "").strip()
        obj = reference
    obj = obj.strip()
    if not obj:
        raise ValueError("empty ccp object")

    base = (cfg.get("ccp.url") or "").strip().rstrip("/")
    app_id = (cfg.get("ccp.app_id") or "").strip()
    if not base or not app_id:
        raise ValueError("ccp.url or ccp.app_id is empty")

    qs = {"AppID": app_id, "Object": obj}
    if safe:
        qs["Safe"] = safe
    url = f"{base}/api/Accounts?" + urllib.parse.urlencode(qs)
    verify_tls = (cfg.get("ccp.verify_tls") or "true").strip().lower() not in (
        "false", "0", "no", "off",
    )
    pem = (cfg.get("ccp.client_cert_pem") or "").strip()

    body = _http_get_json(
        url, headers={"Accept": "application/json"},
        verify_tls=verify_tls, client_cert_pem=pem or None,
    )
    if not isinstance(body, dict) or "Content" not in body:
        raise KeyError(f"ccp: response missing 'Content' for {obj!r}")
    value = body.get("Content")
    if not isinstance(value, str):
        raise TypeError(f"ccp: 'Content' for {obj!r} is not a string")
    return value


# ── Azure Key Vault adapter ──────────────────────────────────────────────────
# Mirror of ``app.utils.secrets._resolve_azurekv``. Stdlib-only so the
# worker image stays free of MSAL.

def _aad_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Acquire (or reuse a cached) Azure AD bearer token for Key Vault."""
    if not tenant_id or not client_id or not client_secret:
        raise ValueError(
            "azurekv: tenant_id / client_id / client_secret missing — "
            "configure secret.azurekv.* in Settings → Compliance"
        )
    cache_key = f"{tenant_id}::{client_id}"
    entry = _aad_token_cache.get(cache_key)
    if entry is not None and entry[0] > time.time():
        return entry[1]

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "https://vault.azure.net/.default",
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"azurekv: AAD token endpoint returned HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"azurekv: AAD token endpoint unreachable: {e.reason}") from e

    token = payload.get("access_token")
    expires_in = payload.get("expires_in", 0)
    if not token:
        raise RuntimeError(f"azurekv: AAD response missing access_token (got {list(payload)!r})")
    expires_at = time.time() + max(60, int(expires_in) - 60)
    _aad_token_cache[cache_key] = (expires_at, token)
    return token


def _resolve_azurekv(reference: str, cfg: dict[str, str]) -> str:
    """Resolve an ``azurekv://<vault>/<secret>`` reference."""
    base_ref = reference
    explicit_version: str | None = None
    if "?" in reference:
        base_ref, qs = reference.split("?", 1)
        params = urllib.parse.parse_qs(qs)
        if "version" in params and params["version"]:
            explicit_version = params["version"][0].strip() or None
    if "/" not in base_ref:
        raise ValueError(
            f"azurekv: malformed reference {reference!r} "
            "(expected 'azurekv://<vault>/<secret>')"
        )
    vault_name, secret_name = base_ref.split("/", 1)
    vault_name = vault_name.strip().strip("/")
    secret_name = secret_name.strip().strip("/")
    if not vault_name or not secret_name:
        raise ValueError(f"azurekv: empty vault or secret name in {reference!r}")

    tenant = (cfg.get("azurekv.tenant_id") or "").strip()
    client_id = (cfg.get("azurekv.client_id") or "").strip()
    client_secret = (cfg.get("azurekv.client_secret") or "").strip()
    api_version = (cfg.get("azurekv.api_version") or "7.4").strip() or "7.4"

    token = _aad_token(tenant, client_id, client_secret)
    path = f"/secrets/{urllib.parse.quote(secret_name, safe='')}"
    if explicit_version:
        path += f"/{urllib.parse.quote(explicit_version, safe='')}"
    url = f"https://{vault_name}.vault.azure.net{path}?api-version={api_version}"

    body = _http_get_json(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if not isinstance(body, dict) or "value" not in body:
        raise KeyError(
            f"azurekv: response missing 'value' for {secret_name!r}"
        )
    value = body.get("value")
    if not isinstance(value, str):
        raise TypeError(f"azurekv: 'value' for {secret_name!r} is not a string")
    return value


# ── AWS Secrets Manager adapter ──────────────────────────────────────────────
# Mirror of ``app.utils.secrets._resolve_awssm`` — stdlib-only SigV4
# signing so the worker image stays free of boto3.

_AWS_SERVICE = "secretsmanager"
_AWS_CONTENT_TYPE = "application/x-amz-json-1.1"


def _aws_sigv4_sign(
    *,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    body: bytes,
    host: str,
    amz_date: str,
    date_stamp: str,
    target: str,
) -> dict[str, str]:
    payload_hash = hashlib.sha256(body).hexdigest()
    headers_to_sign: list[tuple[str, str]] = [
        ("content-type", _AWS_CONTENT_TYPE),
        ("host", host),
        ("x-amz-date", amz_date),
        ("x-amz-target", target),
    ]
    if session_token:
        headers_to_sign.append(("x-amz-security-token", session_token))
    headers_to_sign.sort(key=lambda kv: kv[0])
    canonical_headers = "".join(f"{k}:{v.strip()}\n" for k, v in headers_to_sign)
    signed_headers = ";".join(k for k, _ in headers_to_sign)
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/{_AWS_SERVICE}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, _AWS_SERVICE)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    out = {
        "Content-Type": _AWS_CONTENT_TYPE,
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": target,
        "Authorization": auth,
    }
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    return out


def _resolve_awssm(reference: str, cfg: dict[str, str]) -> str:
    """Resolve an ``awssm://<secret-id>[#<field>]`` reference."""
    import datetime as _dt

    if "#" in reference:
        secret_id, field = reference.split("#", 1)
    else:
        secret_id, field = reference, ""
    secret_id = secret_id.strip().strip("/")
    field = field.strip()
    if not secret_id:
        raise ValueError("awssm: empty secret name")

    region = (cfg.get("awssm.region") or "").strip()
    access_key = (cfg.get("awssm.access_key_id") or "").strip()
    secret_key = (cfg.get("awssm.secret_access_key") or "").strip()
    session_token = (cfg.get("awssm.session_token") or "").strip()
    if not (region and access_key and secret_key):
        raise ValueError(
            "awssm: region / access_key_id / secret_access_key incomplete"
        )

    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = f"{_AWS_SERVICE}.{region}.amazonaws.com"
    body = json.dumps({"SecretId": secret_id}).encode("utf-8")
    headers = _aws_sigv4_sign(
        region=region, access_key=access_key, secret_key=secret_key,
        session_token=session_token, body=body, host=host,
        amz_date=amz_date, date_stamp=date_stamp,
        target="secretsmanager.GetSecretValue",
    )

    req = urllib.request.Request(
        f"https://{host}/", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"awssm: HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"awssm: unreachable: {e.reason}") from e

    if not isinstance(payload, dict):
        raise RuntimeError("awssm: unexpected response shape")
    secret_string = payload.get("SecretString")
    if not isinstance(secret_string, str):
        raise TypeError(f"awssm: secret {secret_id!r} has no SecretString")
    if not field:
        return secret_string
    try:
        parsed = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"awssm: secret {secret_id!r} is not JSON, can't extract field {field!r}"
        ) from exc
    if not isinstance(parsed, dict) or field not in parsed:
        raise KeyError(f"awssm: field {field!r} not present in {secret_id!r}")
    value = parsed[field]
    if not isinstance(value, str):
        raise TypeError(f"awssm: field {field!r} in {secret_id!r} is not a string")
    return value


# ── CyberArk Conjur adapter ──────────────────────────────────────────────────
# Mirror of ``app.utils.secrets._resolve_conjur``. Two-step flow: API-key
# login mints a short-lived token, then secret reads carry that token in
# the Authorization header.

def _conjur_login(
    *,
    url: str, account: str, host_id: str, api_key: str, verify_tls: bool,
) -> str:
    cache_key = f"{url}::{account}::{host_id}"
    entry = _conjur_token_cache.get(cache_key)
    if entry is not None and entry[0] > time.time():
        return entry[1]

    if not all((url, account, host_id, api_key)):
        raise ValueError(
            "conjur: url / account / host_id / api_key incomplete — "
            "configure secret.conjur.* in Settings → Compliance"
        )

    canonical_host = host_id if host_id.startswith("host/") else f"host/{host_id}"
    encoded_host = urllib.parse.quote(canonical_host, safe='')
    encoded_account = urllib.parse.quote(account, safe='')
    login_url = f"{url}/{encoded_account}/host/{encoded_host}/authn"

    ctx: ssl.SSLContext | None = None
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        login_url,
        data=api_key.encode("utf-8"),
        headers={
            "Content-Type": "text/plain",
            "Accept-Encoding": "base64",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=ctx) as resp:
            token = resp.read().decode("ascii").strip()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(
            f"conjur: login failed for host {canonical_host!r}: HTTP {e.code} {detail}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"conjur: login endpoint unreachable: {e.reason}") from e

    if not token:
        raise RuntimeError("conjur: login returned an empty token")
    _conjur_token_cache[cache_key] = (time.time() + _CONJUR_TOKEN_TTL_SECONDS, token)
    return token


def _resolve_conjur(reference: str, cfg: dict[str, str]) -> str:
    """Resolve a ``conjur://<identifier>[#<field>]`` reference."""
    if "#" in reference:
        identifier, field = reference.split("#", 1)
    else:
        identifier, field = reference, ""
    identifier = identifier.strip().strip("/")
    field = field.strip()
    if not identifier:
        raise ValueError("conjur: empty identifier")

    url = (cfg.get("conjur.url") or "").strip().rstrip("/")
    account = (cfg.get("conjur.account") or "").strip()
    host_id = (cfg.get("conjur.host_id") or "").strip()
    api_key = (cfg.get("conjur.api_key") or "").strip()
    verify_tls = (cfg.get("conjur.verify_tls") or "true").strip().lower() not in (
        "false", "0", "no", "off",
    )

    token = _conjur_login(
        url=url, account=account, host_id=host_id,
        api_key=api_key, verify_tls=verify_tls,
    )

    encoded_account = urllib.parse.quote(account, safe='')
    encoded_id = urllib.parse.quote(identifier, safe='')
    secret_url = f"{url}/secrets/{encoded_account}/variable/{encoded_id}"

    ctx: ssl.SSLContext | None = None
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    auth_header = f'Token token="{token}"'
    req = urllib.request.Request(
        secret_url,
        headers={"Authorization": auth_header, "Accept": "*/*"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=ctx) as resp:
            value = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _conjur_token_cache.pop(f"{url}::{account}::{host_id}", None)
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(
            f"conjur: read failed for {identifier!r}: HTTP {e.code} {detail}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"conjur: secret endpoint unreachable: {e.reason}") from e

    if not field:
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"conjur: secret {identifier!r} is not JSON, can't extract field {field!r}"
        ) from exc
    if not isinstance(parsed, dict) or field not in parsed:
        raise KeyError(f"conjur: field {field!r} not present in {identifier!r}")
    extracted = parsed[field]
    if not isinstance(extracted, str):
        raise TypeError(f"conjur: field {field!r} in {identifier!r} is not a string")
    return extracted


def _http_get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    verify_tls: bool = True,
    client_cert_pem: str | None = None,
) -> Any:
    ctx: ssl.SSLContext | None = None
    cert_tempfile: str | None = None
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if client_cert_pem:
        fd, cert_tempfile = tempfile.mkstemp(suffix=".pem", prefix="ipsolis-ccp-")
        try:
            os.write(fd, client_cert_pem.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(cert_tempfile, 0o600)
        if ctx is None:
            ctx = ssl.create_default_context()
        ctx.load_cert_chain(certfile=cert_tempfile)

    try:
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=ctx) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from e
    finally:
        if cert_tempfile:
            try:
                os.unlink(cert_tempfile)
            except Exception:  # noqa: BLE001
                pass
