"""External secret-management adapters — slice 1 (Vault + CyberArk CCP/AIM).

Goal: take the plaintext credentials out of ``app_config`` for tenants
who already invest in a managed secret store. The admin-facing change
is small — a secret-typed ``app_config`` row whose ``value`` is a
recognised reference scheme is resolved to its real value at read time.
Everything else (UI, API, audit) is unchanged.

Reference grammar
-----------------

* ``vault://<path>`` — KV v2 lookup. ``<path>`` is the path *inside*
  the configured KV mount; the resolver prepends the mount + ``/data/``
  automatically. Optional ``#field`` selects a key from the secret's
  ``data.data`` dict; the default is ``value``.

  Example: ``vault://ipsolis/ad/password`` →
  ``GET <vault_url>/v1/<kv_mount>/data/ipsolis/ad/password``.

* ``ccp://[<safe>/]<object>`` — CyberArk Central Credential Provider
  (Application Access Manager) lookup. Resolves via
  ``GET <ccp_url>/api/Accounts?AppID=<app>&Safe=<safe>&Object=<object>``.
  ``<safe>`` defaults to ``secret.ccp.safe`` when omitted.

Anything that doesn't match a known scheme is returned unchanged —
back-compat for plaintext rows, and a soft path for the migration
where some secrets are externalised and others aren't.

Caching
-------

Resolved values are cached process-locally for ``secret.cache_ttl_seconds``
(default 60s). The cache is keyed by ``(backend_id, reference)`` so
re-reads of the same secret in the same minute don't hammer Vault.
A short TTL keeps rotation latency bounded; tenants who rotate
secrets manually shouldn't expect zero-second propagation.

Authentication
--------------

* Vault: static token (``X-Vault-Token`` header). AppRole and
  Kubernetes JWT auth are explicit slice-2 work — they need
  fetched-at-startup token caches and renewal goroutines that don't
  belong in the slice-1 footprint.
* CCP: API-Key or mTLS. mTLS uses the configured client cert PEM
  (cert + key concatenated). When ``secret.ccp.client_cert_pem`` is
  empty, plain HTTPS with no client auth is used (suitable for CCP
  installs that authorise by AppID + IP allow-list).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import AppConfig

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10
KNOWN_SCHEMES = ("vault://", "ccp://", "azurekv://", "awssm://", "conjur://")


# ── Public API ────────────────────────────────────────────────────────────────

def is_secret_reference(value: str | None) -> bool:
    """``True`` when ``value`` is a recognised external-secret reference."""
    if not isinstance(value, str):
        return False
    return any(value.startswith(s) for s in KNOWN_SCHEMES)


async def resolve_secret_value(
    db: AsyncSession,
    raw_value: str | None,
) -> str:
    """Resolve a possibly-reference value to its real secret (async).

    Plain strings → returned as-is (back-compat for plaintext rows).
    ``vault://...`` / ``ccp://...`` → fetched via the configured backend.
    On failure: logs at WARNING and returns the empty string. Callers
    that consider an empty credential a hard failure should validate
    the result themselves; we deliberately don't raise so a transient
    Vault outage doesn't crash an unrelated request.
    """
    if not raw_value or not isinstance(raw_value, str):
        return raw_value or ""
    if not is_secret_reference(raw_value):
        return raw_value

    cached = _cache_get(raw_value)
    if cached is not None:
        return cached

    try:
        cfg = await _load_secret_cfg(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("secrets: failed to load backend config: %s", exc)
        return ""
    return _dispatch_and_cache(raw_value, cfg)


def resolve_secret_value_sync(raw_value: str | None) -> str:
    """Sync sibling of ``resolve_secret_value`` — used by callers that
    aren't on an asyncio loop (the AD-lookup helper, the Celery worker).

    Loads ``secret.*`` config via a psycopg2 connection on every call
    when it can't reuse the cache. Same back-compat semantics as the
    async version: plain strings pass through, references resolve.
    """
    if not raw_value or not isinstance(raw_value, str):
        return raw_value or ""
    if not is_secret_reference(raw_value):
        return raw_value

    cached = _cache_get(raw_value)
    if cached is not None:
        return cached

    try:
        cfg = _load_secret_cfg_sync()
    except Exception as exc:  # noqa: BLE001
        logger.warning("secrets: failed to load backend config (sync): %s", exc)
        return ""
    return _dispatch_and_cache(raw_value, cfg)


def _dispatch_and_cache(raw_value: str, cfg: dict[str, str]) -> str:
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
    _cache_set(raw_value, value, ttl_seconds=int(cfg.get("cache_ttl_seconds", 60) or 60))
    return value


# ── Cache (process-local, TTL'd) ──────────────────────────────────────────────

# Tiny TTL cache. We accept the standard caveats (no LRU eviction; lives
# for the process lifetime; not shared across api/worker replicas) — the
# size is bounded by the number of distinct secret references actually
# read by this process, which is a small constant in practice.
_cache: dict[str, tuple[float, str]] = {}


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


def cache_clear() -> None:
    """Drop all cached resolutions. Called by the test endpoint and
    after rotation operations so the next read goes back to source."""
    _cache.clear()


# ── Config loading ────────────────────────────────────────────────────────────

async def _load_secret_cfg(db: AsyncSession) -> dict[str, str]:
    """Read ``secret.*`` keys into a flat dict (suffix-only key names)."""
    rows = await db.execute(
        select(AppConfig.key, AppConfig.value).where(AppConfig.key.like("secret.%"))
    )
    cfg: dict[str, str] = {}
    for key, value in rows.all():
        # ``secret.vault.url`` → ``vault.url``; bare ``secret.backend`` → ``backend``.
        cfg[key[len("secret."):]] = value or ""
    return cfg


def _load_secret_cfg_sync() -> dict[str, str]:
    """Sync sibling — psycopg2 read of ``secret.*`` keys.

    Falls back gracefully when ``DATABASE_URL`` is missing or the table
    isn't reachable: returns empty dict, which makes the resolver
    treat every reference as unresolvable (returns empty string). That
    matches the "fail closed but quiet" contract of the resolver.
    """
    import os  # noqa: PLC0415

    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        return {}
    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        import psycopg2  # noqa: PLC0415
    except ImportError:
        return {}
    try:
        conn = psycopg2.connect(sync_url)
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM app_config WHERE key LIKE 'secret.%%'")
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("secrets: psycopg2 fetch failed: %s", exc)
        return {}
    out: dict[str, str] = {}
    for key, value in rows:
        out[key[len("secret."):]] = value or ""
    return out


# ── Vault adapter ─────────────────────────────────────────────────────────────

def _resolve_vault(path: str, cfg: dict[str, str]) -> str:
    """Resolve a ``vault://<path>[#<field>]`` reference.

    Path is interpreted under the configured KV v2 mount. The optional
    ``#field`` fragment selects a single key from the secret's data
    dict; ``value`` is the default and matches the convention for
    "the secret is just a string under key 'value'".
    """
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
    headers = {
        "X-Vault-Token": token,
        "Accept": "application/json",
    }
    if namespace:
        headers["X-Vault-Namespace"] = namespace

    body = _http_get_json(url, headers=headers)
    # KV v2 envelope: ``{"data": {"data": {...}, "metadata": {...}}}``.
    inner = (((body or {}).get("data") or {}).get("data") or {})
    if field not in inner:
        raise KeyError(f"vault: field {field!r} not present at {path!r}")
    value = inner[field]
    if not isinstance(value, str):
        raise TypeError(f"vault: field {field!r} at {path!r} is not a string")
    return value


# ── CyberArk CCP / AIM adapter ────────────────────────────────────────────────

def _resolve_ccp(reference: str, cfg: dict[str, str]) -> str:
    """Resolve a ``ccp://[<safe>/]<object>`` reference.

    Slice-1 contract: returns the secret's ``Content`` field — the
    canonical "password" for the account. CCP also returns metadata
    (Address, UserName, …) which a future slice could expose via a
    ``#field`` fragment if useful.

    mTLS is optional. When ``secret.ccp.client_cert_pem`` is set, the
    PEM (cert + key) is materialised to a temp file with mode 0600
    just for the duration of the request. CCP installs that gate by
    AppID + IP allow-list alone leave the field empty.
    """
    if "/" in reference:
        safe, obj = reference.split("/", 1)
    else:
        safe = (cfg.get("ccp.safe") or "").strip()
        obj = reference
    safe = safe.strip()
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
        url,
        headers={"Accept": "application/json"},
        verify_tls=verify_tls,
        client_cert_pem=pem or None,
    )
    if not isinstance(body, dict) or "Content" not in body:
        raise KeyError(f"ccp: response missing 'Content' for {obj!r} (got {list(body)!r})")
    value = body.get("Content")
    if not isinstance(value, str):
        raise TypeError(f"ccp: 'Content' for {obj!r} is not a string")
    return value


# ── Azure Key Vault adapter ──────────────────────────────────────────────────

# Azure AD bearer-token cache, separate from the secret-value cache
# because tokens have their own (provider-driven) expiry and are
# shared across all secrets read with the same SPN. Keyed by tenant_id
# so multi-tenant config drift can't accidentally cross-pollinate
# tokens between tenants.
_aad_token_cache: dict[str, tuple[float, str]] = {}


def _aad_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Acquire (or reuse a cached) Azure AD bearer token for Key Vault.

    Uses the OAuth 2.0 client_credentials flow against the v2.0 token
    endpoint — same shape MSAL would produce, but stdlib-only so the
    worker mirror doesn't pull MSAL into its image. Tokens cache for
    ``expires_in - 60s`` to give a 60-second safety margin against
    clock skew at the wire.
    """
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
    # Refresh 60s before expiry to avoid wire-time clock-skew races.
    expires_at = time.time() + max(60, int(expires_in) - 60)
    _aad_token_cache[cache_key] = (expires_at, token)
    return token


def _resolve_azurekv(reference: str, cfg: dict[str, str]) -> str:
    """Resolve an ``azurekv://<vault-name>/<secret-name>`` reference.

    Slice-1 contract: returns the secret's ``value`` field, which is
    the canonical "this is the secret string" for an Azure KV secret.
    Versioned references (``?version=<id>``) are accepted but optional
    — omitted resolves to the latest version.
    """
    # Strip optional querystring for version selection.
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
        raise ValueError(
            f"azurekv: empty vault or secret name in {reference!r}"
        )

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
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    if not isinstance(body, dict) or "value" not in body:
        shape = repr(list(body)) if isinstance(body, dict) else type(body).__name__
        raise KeyError(
            f"azurekv: response missing 'value' for {secret_name!r} (got {shape})"
        )
    value = body.get("value")
    if not isinstance(value, str):
        raise TypeError(f"azurekv: 'value' for {secret_name!r} is not a string")
    return value


# ── AWS Secrets Manager adapter ──────────────────────────────────────────────
#
# Auth: AWS Signature Version 4 against the Secrets Manager regional
# endpoint (``secretsmanager.<region>.amazonaws.com``). SigV4 needs no
# external SDK — the canonical-request → string-to-sign → derived
# signing-key sequence is well-defined and short. We do it inline so
# the worker mirror doesn't pull boto3 (~10MB) into its image just to
# read a couple of secrets.
#
# References:
#   - https://docs.aws.amazon.com/general/latest/gr/sigv4-create-canonical-request.html
#   - https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html

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
    """Build the SigV4 ``Authorization`` header + supporting headers.

    Returns a dict ready to merge into the request headers. ``amz_date``
    is the ISO 8601 basic-format timestamp (``20260430T123456Z``);
    ``date_stamp`` is the ``YYYYMMDD`` portion. Both are computed by
    the caller from the same UTC moment to ensure consistency.

    ``target`` is the ``X-Amz-Target`` header value — e.g.
    ``secretsmanager.GetSecretValue`` or ``secretsmanager.ListSecrets``.
    """
    # Step 1: canonical request.
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_uri = "/"
    canonical_query = ""
    # Headers MUST be lowercased + sorted. Include x-amz-security-token
    # only when a session token is present.
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
        f"POST\n{canonical_uri}\n{canonical_query}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    # Step 2: string to sign.
    credential_scope = f"{date_stamp}/{region}/{_AWS_SERVICE}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    # Step 3: derive signing key (4-step HMAC chain).
    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, _AWS_SERVICE)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    # Step 4: assemble Authorization header.
    auth = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    out_headers = {
        "Content-Type": _AWS_CONTENT_TYPE,
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": target,
        "Authorization": auth,
    }
    if session_token:
        out_headers["X-Amz-Security-Token"] = session_token
    return out_headers


def _aws_sm_get_secret(
    *,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    secret_id: str,
) -> dict[str, Any]:
    """POST a SigV4-signed ``GetSecretValue`` and return the parsed body.

    Raises ``RuntimeError`` on any non-2xx response with the AWS error
    message included so config errors surface in the test endpoint /
    log line.
    """
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = f"{_AWS_SERVICE}.{region}.amazonaws.com"
    url = f"https://{host}/"
    body = json.dumps({"SecretId": secret_id}).encode("utf-8")
    headers = _aws_sigv4_sign(
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        body=body,
        host=host,
        amz_date=amz_date,
        date_stamp=date_stamp,
        target="secretsmanager.GetSecretValue",
    )

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"awssm: HTTP {e.code} from Secrets Manager: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"awssm: Secrets Manager unreachable: {e.reason}") from e
    if not isinstance(payload, dict):
        raise RuntimeError(f"awssm: unexpected response shape: {type(payload).__name__}")
    return payload


def _resolve_awssm(reference: str, cfg: dict[str, str]) -> str:
    """Resolve an ``awssm://<secret-id>[#<field>]`` reference.

    ``<secret-id>`` is the friendly name (e.g. ``ad/bind-password``) or
    the secret-name portion of a full ARN. Resolves against the
    configured ``secret.awssm.region``. Cross-region references via
    explicit ARN are queued for slice 2.

    The fragment ``#<field>`` mirrors the Vault convention: when the
    secret is a JSON-stringified blob (common AWS pattern,
    ``{"username":"foo","password":"bar"}``), pulls the named key out.
    Without a fragment we return ``SecretString`` as-is.
    """
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
            "awssm: region / access_key_id / secret_access_key incomplete — "
            "configure secret.awssm.* in Settings → Compliance"
        )

    body = _aws_sm_get_secret(
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        secret_id=secret_id,
    )

    secret_string = body.get("SecretString")
    if not isinstance(secret_string, str):
        # SecretBinary path — base64 in the JSON. Slice 1 doesn't decode
        # it; binary secrets are uncommon for credential storage.
        raise TypeError(
            f"awssm: secret {secret_id!r} has no SecretString "
            "(binary secrets aren't supported in slice 1)"
        )

    if not field:
        return secret_string

    # JSON field extraction.
    try:
        parsed = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"awssm: secret {secret_id!r} is not JSON, can't extract field {field!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"awssm: secret {secret_id!r} JSON is not an object, can't extract field {field!r}"
        )
    if field not in parsed:
        raise KeyError(f"awssm: field {field!r} not present in {secret_id!r}")
    value = parsed[field]
    if not isinstance(value, str):
        raise TypeError(f"awssm: field {field!r} in {secret_id!r} is not a string")
    return value


# ── CyberArk Conjur adapter ──────────────────────────────────────────────────
#
# Two-step flow: API-key login mints a short-lived token, then secret reads
# carry that token in the Authorization header. Tokens default to an
# 8-minute TTL on Conjur side; we cache for 7 minutes to leave a 1-minute
# safety margin against clock skew at the wire.
#
# References:
#   - https://docs.cyberark.com/conjur-cloud/Latest/en/Content/Developer/Conjur_API_Authenticate.htm
#   - https://docs.cyberark.com/conjur-cloud/Latest/en/Content/Developer/Conjur_API_Retrieve_Secret.htm

_CONJUR_TOKEN_TTL_SECONDS = 7 * 60  # tokens last 8 minutes; refresh a minute early
_conjur_token_cache: dict[str, tuple[float, str]] = {}


def _conjur_login(
    *,
    url: str, account: str, host_id: str, api_key: str, verify_tls: bool,
) -> str:
    """Acquire (or reuse a cached) Conjur access token via host API-key auth.

    Cache key includes the URL + account + host so config drift can't
    cross-pollinate tokens between tenants on a shared resolver process.
    """
    cache_key = f"{url}::{account}::{host_id}"
    entry = _conjur_token_cache.get(cache_key)
    if entry is not None and entry[0] > time.time():
        return entry[1]

    if not all((url, account, host_id, api_key)):
        raise ValueError(
            "conjur: url / account / host_id / api_key incomplete — "
            "configure secret.conjur.* in Settings → Compliance"
        )

    # Conjur expects ``host/<id>`` for host-scoped auth. Allow operators
    # to type just the bare id (most common config-form mistake).
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
            # Asking for base64 makes Conjur return the raw token in the body
            # — significantly easier than parsing the default JSON envelope.
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
    """Resolve a ``conjur://<identifier>[#<field>]`` reference.

    Identifier may contain slashes (e.g. ``prod/ipsolis/ad-bind-password``);
    we URL-encode it as a single path segment so Conjur treats nested
    namespacing correctly.

    Returns the raw secret value, or — when ``#field`` is present — the
    named field after parsing the value as JSON. The JSON convention
    mirrors AWS SM and supports the common pattern of storing a creds
    blob like ``{"username":"…","password":"…"}`` as one variable.
    """
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

    # Conjur's Authorization header format: Token token="<base64>"
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
        # 401 typically means the cached token expired between login and
        # read (rare, since we refresh a minute early). Drop the cache
        # entry so the next call re-mints; this call still fails.
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
    # JSON field extraction (mirror of AWS SM and Vault conventions).
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


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _http_get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    verify_tls: bool = True,
    client_cert_pem: str | None = None,
) -> Any:
    """Synchronous GET returning parsed JSON. Used inside ``run_in_executor``
    by the async resolvers so we avoid pulling httpx into the runtime
    deps for what is at most a couple of small calls per request.

    ``client_cert_pem`` materialises the cert + key to a 0600 temp file
    for the lifetime of the call (Python's stdlib ssl needs paths, not
    in-memory PEM blobs).
    """
    ctx: ssl.SSLContext | None = None
    cert_tempfile: str | None = None

    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if client_cert_pem:
        # mkstemp with mode 0600 — the file holds a private key, so we
        # can't use a tempfile.NamedTemporaryFile (which is 0600 on
        # POSIX but not portable). Best-effort cleanup in finally.
        import os
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
                import os
                os.unlink(cert_tempfile)
            except Exception:  # noqa: BLE001
                pass


# ── Test connection ───────────────────────────────────────────────────────────

async def test_backend(db: AsyncSession) -> tuple[bool, str]:
    """Verify backend connectivity. Returns ``(ok, message)``.

    Per-backend probe strategy:

    * ``vault``  — ``/v1/sys/health`` (no token required, works on dev + ent).
    * ``ccp``    — ``/api/Verify`` with optional client cert.
    * ``azurekv``— acquire an AAD token for ``vault.azure.net`` (no vault probe).
    * ``awssm``  — ``ListSecrets`` with ``MaxResults=1`` (exercises SigV4 + IAM).
    * ``conjur`` — host API-key login (mints a fresh access token).
    * ``db``     — always ok (no external state).

    The cheaper backends (azurekv / conjur) probe authentication
    only because the most common operator error is wrong creds —
    not "the vault is missing". Probing a real secret would require
    a secret name we don't have here and would muddy the error.
    """
    cfg = await _load_secret_cfg(db)
    backend = (cfg.get("backend") or "db").strip().lower()
    if backend == "db":
        return True, "Backend 'db' — no external store configured."
    if backend == "vault":
        base = (cfg.get("vault.url") or "").strip().rstrip("/")
        if not base:
            return False, "vault.url is empty."
        try:
            _http_get_json(f"{base}/v1/sys/health", headers={"Accept": "application/json"})
            return True, "Vault reachable (sys/health responded)."
        except Exception as exc:  # noqa: BLE001
            return False, f"Vault unreachable: {exc}"
    if backend == "ccp":
        base = (cfg.get("ccp.url") or "").strip().rstrip("/")
        app_id = (cfg.get("ccp.app_id") or "").strip()
        if not base or not app_id:
            return False, "ccp.url or ccp.app_id is empty."
        verify_tls = (cfg.get("ccp.verify_tls") or "true").strip().lower() not in (
            "false", "0", "no", "off",
        )
        pem = (cfg.get("ccp.client_cert_pem") or "").strip()
        try:
            # CCP exposes /api/Verify on most builds; fall back to the
            # AppID probe on the off chance the install is older.
            _http_get_json(
                f"{base}/api/Verify",
                headers={"Accept": "application/json"},
                verify_tls=verify_tls,
                client_cert_pem=pem or None,
            )
            return True, "CCP reachable (Verify responded)."
        except RuntimeError as exc:
            # Treat any 2xx-or-4xx as "reachable" since /api/Verify
            # may legitimately reject without a request body but the
            # network path is up. 5xx and connection errors mean
            # genuinely down.
            msg = str(exc)
            if "HTTP 4" in msg:
                return True, f"CCP reachable but Verify returned: {msg}"
            return False, f"CCP unreachable: {msg}"
    if backend == "azurekv":
        tenant = (cfg.get("azurekv.tenant_id") or "").strip()
        client_id = (cfg.get("azurekv.client_id") or "").strip()
        client_secret = (cfg.get("azurekv.client_secret") or "").strip()
        if not (tenant and client_id and client_secret):
            return False, "azurekv: tenant_id / client_id / client_secret incomplete."
        # Verify the SPN can acquire a Key Vault token. Doesn't probe
        # any specific vault — that requires a vault name we don't
        # have here, and the most common config error is "the SPN
        # itself is misconfigured" rather than "the vault is down".
        try:
            _aad_token_cache.pop(f"{tenant}::{client_id}", None)
            token = _aad_token(tenant, client_id, client_secret)
            return True, (
                f"Azure AD reachable, SPN authenticated against "
                f"https://vault.azure.net (token len={len(token)})."
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"Azure KV auth failed: {exc}"
    if backend == "awssm":
        region = (cfg.get("awssm.region") or "").strip()
        access_key = (cfg.get("awssm.access_key_id") or "").strip()
        secret_key = (cfg.get("awssm.secret_access_key") or "").strip()
        session_token = (cfg.get("awssm.session_token") or "").strip()
        if not (region and access_key and secret_key):
            return False, "awssm: region / access_key_id / secret_access_key incomplete."
        # Probe by attempting to list secrets — the cheapest call that
        # exercises both SigV4 signing and the IAM principal's policy.
        # Empty result is fine; we only care about the auth success.
        try:
            _aws_sm_list(
                region=region, access_key=access_key,
                secret_key=secret_key, session_token=session_token,
            )
            return True, f"AWS Secrets Manager reachable in {region} (SigV4 OK)."
        except Exception as exc:  # noqa: BLE001
            # Surface AWS error codes (AccessDeniedException, InvalidSignatureException)
            # so config typos / missing IAM policy show up clearly.
            return False, f"AWS Secrets Manager auth failed: {exc}"
    if backend == "conjur":
        url = (cfg.get("conjur.url") or "").strip().rstrip("/")
        account = (cfg.get("conjur.account") or "").strip()
        host_id = (cfg.get("conjur.host_id") or "").strip()
        api_key = (cfg.get("conjur.api_key") or "").strip()
        if not (url and account and host_id and api_key):
            return False, (
                "conjur: url / account / host_id / api_key incomplete."
            )
        verify_tls = (cfg.get("conjur.verify_tls") or "true").strip().lower() not in (
            "false", "0", "no", "off",
        )
        # Probe by exercising the host login flow. Doesn't read a
        # specific secret — that needs an identifier we don't have
        # here, and the most common configuration error is "wrong
        # api_key for this host" rather than "secret missing".
        try:
            _conjur_token_cache.pop(f"{url}::{account}::{host_id}", None)
            token = _conjur_login(
                url=url, account=account, host_id=host_id,
                api_key=api_key, verify_tls=verify_tls,
            )
            return True, (
                f"Conjur reachable, host {host_id!r} authenticated "
                f"(token len={len(token)})."
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"Conjur auth failed: {exc}"
    return False, f"Unknown backend: {backend!r}"


def _aws_sm_list(
    *,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
) -> None:
    """Cheap probe — calls ``ListSecrets`` with ``MaxResults=1`` to
    exercise the SigV4 signing + IAM auth path. Discards the result.

    Different X-Amz-Target than ``GetSecretValue`` so the IAM policy
    needs both ``secretsmanager:ListSecrets`` AND
    ``secretsmanager:GetSecretValue`` for full ipSolis use. The
    setup guide calls this out so admins don't get a surprise 403
    on first secret read after a successful test.
    """
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = f"{_AWS_SERVICE}.{region}.amazonaws.com"
    body = json.dumps({"MaxResults": 1}).encode("utf-8")
    headers = _aws_sigv4_sign(
        region=region, access_key=access_key, secret_key=secret_key,
        session_token=session_token, body=body, host=host,
        amz_date=amz_date, date_stamp=date_stamp,
        target="secretsmanager.ListSecrets",
    )
    req = urllib.request.Request(f"https://{host}/", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            _ = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"HTTP {e.code} from Secrets Manager: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"unreachable: {e.reason}") from e
