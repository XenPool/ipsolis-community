"""Tests for the multi-key license trust list and verification pipeline.

All tests use freshly-generated test keypairs — no production keys required.
Production fixture files (tests/fixtures/*.lic) are tested separately and
skipped when absent so the CI suite runs without private key material.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

# ── Helpers ─────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures"


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_ed25519_pair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    raw = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, raw


def sign_ed25519(priv, payload: dict) -> str:
    sig = priv.sign(canonical(payload))
    return base64.b64encode(sig).decode()


# ── Fixtures (pytest) ────────────────────────────────────────────────────────

@pytest.fixture()
def test_ed25519(monkeypatch):
    """Inject a fresh Ed25519 test key into the trust list."""
    from app.license import trusted_keys as tk
    from app.license.trusted_keys import TrustedKey

    priv, raw = make_ed25519_pair()
    test_key = TrustedKey(
        key_id="test-ed25519",
        algorithm="ed25519",
        public_key_pem=None,
        public_key_raw=raw,
        rsa_padding=None,
        rsa_hash=None,
        accepted_license_types=frozenset({"demo"}),
        description="Test Ed25519 key",
        deprecated_after=None,
    )
    original_keys = tk.TRUSTED_KEYS[:]
    original_by_id = dict(tk.TRUSTED_KEYS_BY_ID)
    tk.TRUSTED_KEYS.append(test_key)
    tk.TRUSTED_KEYS_BY_ID[test_key.key_id] = test_key
    yield priv, test_key
    tk.TRUSTED_KEYS[:] = original_keys
    tk.TRUSTED_KEYS_BY_ID.clear()
    tk.TRUSTED_KEYS_BY_ID.update(original_by_id)


@pytest.fixture()
def test_commercial_ed25519(monkeypatch):
    """Inject a fresh Ed25519 test key for commercial licenses into the trust list."""
    from app.license import trusted_keys as tk
    from app.license.trusted_keys import TrustedKey

    priv, raw = make_ed25519_pair()
    test_key = TrustedKey(
        key_id="test-commercial-ed25519",
        algorithm="ed25519",
        public_key_pem=None,
        public_key_raw=raw,
        rsa_padding=None,
        rsa_hash=None,
        accepted_license_types=frozenset({"commercial"}),
        description="Test commercial Ed25519 key",
        deprecated_after=None,
    )
    original_keys = tk.TRUSTED_KEYS[:]
    original_by_id = dict(tk.TRUSTED_KEYS_BY_ID)
    tk.TRUSTED_KEYS.append(test_key)
    tk.TRUSTED_KEYS_BY_ID[test_key.key_id] = test_key
    yield priv, test_key
    tk.TRUSTED_KEYS[:] = original_keys
    tk.TRUSTED_KEYS_BY_ID.clear()
    tk.TRUSTED_KEYS_BY_ID.update(original_by_id)


# ── Unit tests ───────────────────────────────────────────────────────────────

def test_ed25519_legacy_no_key_id_verifies(test_ed25519):
    """Legacy license (no key_id) signed with Ed25519 verifies via fallback path."""
    from app.license.verify import verify_license_payload
    priv, key = test_ed25519
    payload = {
        "license_id": "lic-001",
        "licensee": "Acme",
        "edition": "enterprise",
        "max_users": 0,
        "max_asset_types": 0,
        "issued_at": "2025-01-01T00:00:00+00:00",
        "expires_at": "2099-12-31T00:00:00+00:00",
        "features": ["all"],
        # No key_id — legacy
    }
    sig_b64 = sign_ed25519(priv, payload)
    sig_bytes = base64.b64decode(sig_b64)
    result = verify_license_payload(payload, sig_bytes)
    assert result.verified
    assert result.key is not None
    assert result.key.key_id == key.key_id


def test_commercial_ed25519_with_key_id_verifies(test_commercial_ed25519):
    """Commercial license (key_id present) signed with Ed25519 verifies via routed path."""
    from app.license.verify import verify_license_payload
    priv, key = test_commercial_ed25519
    payload = {
        "key_id": key.key_id,
        "type": "commercial",
        "license_id": "lic-002",
        "licensee": "Acme",
        "edition": "enterprise",
        "max_users": 0,
        "max_asset_types": 0,
        "issued_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-12-31T00:00:00+00:00",
        "features": ["all"],
    }
    sig_b64 = sign_ed25519(priv, payload)
    sig_bytes = base64.b64decode(sig_b64)
    result = verify_license_payload(payload, sig_bytes)
    assert result.verified
    assert result.key is not None
    assert result.key.key_id == key.key_id


def test_demo_key_rejects_commercial_type(test_ed25519):
    """A demo-key signature on a payload claiming type='commercial' is rejected."""
    from app.license.verify import verify_license_payload
    priv, key = test_ed25519
    payload = {
        "key_id": key.key_id,
        "type": "commercial",   # not in key.accepted_license_types ({"demo"})
        "license_id": "lic-003",
        "licensee": "Attacker",
        "edition": "enterprise",
        "max_users": 0,
        "max_asset_types": 0,
        "issued_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-12-31T00:00:00+00:00",
        "features": ["all"],
    }
    sig_b64 = sign_ed25519(priv, payload)
    sig_bytes = base64.b64decode(sig_b64)
    result = verify_license_payload(payload, sig_bytes)
    assert not result.verified
    assert "not accepted" in result.reason


def test_unknown_key_id_rejected():
    """A license with an unknown key_id is immediately rejected."""
    from app.license.verify import verify_license_payload
    payload = {
        "key_id": "nonexistent-key",
        "license_id": "lic-004",
        "licensee": "Nobody",
        "edition": "enterprise",
        "issued_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2099-12-31T00:00:00+00:00",
        "features": [],
    }
    result = verify_license_payload(payload, b"\x00" * 64)
    assert not result.verified
    assert "unknown key_id" in result.reason


def test_corrupted_signature_rejected(test_ed25519):
    """A payload with a one-byte-corrupted signature is rejected."""
    from app.license.verify import verify_license_payload
    priv, key = test_ed25519
    payload = {
        "license_id": "lic-005",
        "licensee": "Acme",
        "edition": "enterprise",
        "max_users": 0,
        "max_asset_types": 0,
        "issued_at": "2025-01-01T00:00:00+00:00",
        "expires_at": "2099-12-31T00:00:00+00:00",
        "features": ["all"],
    }
    sig_b64 = sign_ed25519(priv, payload)
    sig_bytes = bytearray(base64.b64decode(sig_b64))
    sig_bytes[0] ^= 0xFF  # corrupt one byte
    result = verify_license_payload(payload, bytes(sig_bytes))
    assert not result.verified


def test_missing_type_falls_back_to_demo(test_ed25519):
    """Legacy payloads without a 'type' field are treated as type='demo'."""
    from app.license.verify import verify_license_payload
    priv, key = test_ed25519
    payload = {
        "license_id": "lic-006",
        "licensee": "Acme",
        "edition": "enterprise",
        "max_users": 0,
        "max_asset_types": 0,
        "issued_at": "2025-01-01T00:00:00+00:00",
        "expires_at": "2099-12-31T00:00:00+00:00",
        "features": [],
        # No 'type' field — must default to "demo"
    }
    sig_b64 = sign_ed25519(priv, payload)
    sig_bytes = base64.b64decode(sig_b64)
    result = verify_license_payload(payload, sig_bytes)
    assert result.verified
    assert result.key is not None
    assert "demo" in result.key.accepted_license_types


def test_trust_list_endpoint_returns_all_keys(test_ed25519, test_commercial_ed25519):
    """GET /admin/license/trust-list returns an entry for every trusted key."""
    from app.license.trusted_keys import TRUSTED_KEYS
    from app.routes.admin_license import license_trust_list
    import asyncio

    result = asyncio.get_event_loop().run_until_complete(license_trust_list())
    key_ids = {entry["key_id"] for entry in result}
    assert all(k.key_id in key_ids for k in TRUSTED_KEYS)
    # Raw key material must not appear
    for entry in result:
        assert "public_key_pem" not in entry
        assert "public_key_raw" not in entry


def test_verified_by_propagates_to_license_info(test_ed25519, tmp_path, monkeypatch):
    """After loading a valid license, LicenseInfo.verified_by_key_id is set."""
    import app.utils.license as lu
    priv, key = test_ed25519
    payload = {
        "license_id": "lic-007",
        "licensee": "Acme",
        "edition": "enterprise",
        "max_users": 0,
        "max_asset_types": 0,
        "issued_at": "2025-01-01T00:00:00+00:00",
        "expires_at": "2099-12-31T00:00:00+00:00",
        "features": ["all"],
    }
    sig_b64 = sign_ed25519(priv, payload)
    lic_file = tmp_path / "ipsolis.lic"
    lic_file.write_text(json.dumps({**payload, "signature": sig_b64}), encoding="utf-8")

    monkeypatch.setattr(lu, "LICENSE_PATH", lic_file)
    monkeypatch.setattr(lu, "_CACHED_INFO", None)
    monkeypatch.setattr(lu, "_CACHED_MTIME", None)

    info = lu.load_license(force_reload=True)
    assert info.valid
    assert info.verified_by_key_id == key.key_id
    assert info.verified_by_description == key.description


# ── Operator YAML override test ──────────────────────────────────────────────

def test_operator_yaml_appends_key(tmp_path, monkeypatch):
    """A valid /etc/ipsolis/trusted_keys.yaml entry is added to the trust list."""
    pytest.importorskip("yaml")
    from app.license import trusted_keys as tk
    import app.license.operator_keys as ok

    priv, raw = make_ed25519_pair()
    yaml_content = f"""\
- key_id: operator-test-key
  algorithm: ed25519
  description: Operator test
  accepted_license_types:
    - demo
  public_key_hex: "{raw.hex()}"
"""
    yaml_file = tmp_path / "trusted_keys.yaml"
    yaml_file.write_text(yaml_content, encoding="utf-8")
    monkeypatch.setattr(ok, "OPERATOR_KEYS_PATH", yaml_file)

    # Reset trust list to bundled state before loading
    original_keys = tk.TRUSTED_KEYS[:]
    original_by_id = dict(tk.TRUSTED_KEYS_BY_ID)
    try:
        ok.load_operator_keys()
        assert "operator-test-key" in tk.TRUSTED_KEYS_BY_ID
        op_key = tk.TRUSTED_KEYS_BY_ID["operator-test-key"]

        from app.license.verify import verify_license_payload
        payload = {
            "license_id": "op-001",
            "licensee": "Operator",
            "edition": "enterprise",
            "max_users": 0,
            "max_asset_types": 0,
            "issued_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-12-31T00:00:00+00:00",
            "features": [],
        }
        sig_b64 = sign_ed25519(priv, payload)
        result = verify_license_payload(payload, base64.b64decode(sig_b64))
        assert result.verified
        assert result.key is not None
        assert result.key.key_id == "operator-test-key"
    finally:
        tk.TRUSTED_KEYS[:] = original_keys
        tk.TRUSTED_KEYS_BY_ID.clear()
        tk.TRUSTED_KEYS_BY_ID.update(original_by_id)


# ── Production fixture tests (skipped when fixture files absent) ─────────────

@pytest.mark.skipif(not (FIXTURES / "demo_legacy.lic").exists(), reason="demo_legacy.lic fixture not generated")
def test_production_demo_legacy_fixture():
    """Real demo_legacy.lic (signed with production Ed25519 key) verifies."""
    from app.license.verify import verify_license_payload
    data = json.loads((FIXTURES / "demo_legacy.lic").read_text())
    sig_bytes = base64.b64decode(data.pop("signature"))
    result = verify_license_payload(data, sig_bytes)
    assert result.verified
    assert result.key is not None
    assert result.key.key_id == "demo-legacy"


@pytest.mark.skipif(not (FIXTURES / "commercial_2026.lic").exists(), reason="commercial_2026.lic fixture not generated")
def test_production_commercial_fixture():
    """Real commercial_2026.lic (signed with production Ed25519 key) verifies."""
    from app.license.verify import verify_license_payload
    data = json.loads((FIXTURES / "commercial_2026.lic").read_text())
    sig_bytes = base64.b64decode(data.pop("signature"))
    result = verify_license_payload(data, sig_bytes)
    assert result.verified
    assert result.key is not None
    assert result.key.key_id == "commercial-2026"
