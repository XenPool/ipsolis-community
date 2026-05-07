"""Helper script to sign test license fixture files.

Run from the api/ directory with both production Ed25519 private keys available:

    python tests/fixtures/generate_license.py

The generated .lic files are committed as static test fixtures so the test
suite doesn't need private key material to run.

Both demo-legacy and commercial-2026 use Ed25519.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

HERE = Path(__file__).parent


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_ed25519(payload: dict, private_key_pem_path: Path) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pem = private_key_pem_path.read_bytes()
    key = load_pem_private_key(pem, password=None)
    sig = key.sign(canonical(payload))  # type: ignore[union-attr]
    return base64.b64encode(sig).decode("ascii")


def write_fixture(name: str, payload: dict, signature: str) -> None:
    out = {**payload, "signature": signature}
    path = HERE / f"{name}.lic"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"  wrote {path}")


def main() -> None:
    demo_key = Path("tools/license/private_key.pem")
    commercial_key = Path("tools/license/commercial-2026-ed25519-private.pem")

    # ── demo-legacy fixture (Ed25519, no key_id) ────────────────────────────
    if demo_key.exists():
        demo_payload = {
            "license_id": "test-demo-001",
            "licensee": "Test Organisation",
            "edition": "enterprise",
            "max_users": 0,
            "max_asset_types": 0,
            "issued_at": "2025-01-01T00:00:00+00:00",
            "expires_at": "2099-12-31T23:59:59+00:00",
            "features": ["all"],
        }
        sig = sign_ed25519(demo_payload, demo_key)
        write_fixture("demo_legacy", demo_payload, sig)

        # tampered: same payload + valid sig but one byte of sig corrupted
        tampered_sig = list(base64.b64decode(sig))
        tampered_sig[0] ^= 0xFF
        write_fixture("tampered", demo_payload, base64.b64encode(bytes(tampered_sig)).decode())
    else:
        print(f"  SKIP demo fixtures: {demo_key} not found")

    # ── commercial-2026 fixture (Ed25519, key_id present) ───────────────────
    if commercial_key.exists():
        commercial_payload = {
            "key_id": "commercial-2026",
            "type": "commercial",
            "license_id": "test-commercial-001",
            "licensee": "Test Organisation",
            "edition": "enterprise",
            "max_users": 0,
            "max_asset_types": 0,
            "issued_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-12-31T23:59:59+00:00",
            "features": ["all"],
        }
        sig = sign_ed25519(commercial_payload, commercial_key)
        write_fixture("commercial_2026", commercial_payload, sig)
    else:
        print(f"  SKIP commercial fixture: {commercial_key} not found")


if __name__ == "__main__":
    main()
