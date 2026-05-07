"""Bundled public-key trust list for ip·Solis license verification.

Each ``TrustedKey`` entry describes one authority whose signatures the backend
will accept.  The list is loaded once at import time; operator-added keys
(``/etc/ipsolis/trusted_keys.yaml``) are appended by ``operator_keys.py``
during application startup.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    """Stable, unique identifier — must match ``key_id`` in the license payload
    (absent on legacy licenses)."""

    algorithm: Literal["rsa", "ed25519"]

    public_key_pem: Optional[str]
    """PEM-encoded SPKI public key.  Set for RSA keys; None for Ed25519."""

    public_key_raw: Optional[bytes]
    """32-byte raw public key.  Set for Ed25519 keys; None for RSA."""

    rsa_padding: Optional[Literal["pss", "pkcs1v15"]]
    """RSA padding scheme.  None for Ed25519."""

    rsa_hash: Optional[Literal["sha256", "sha512"]]
    """RSA hash algorithm.  None for Ed25519."""

    accepted_license_types: frozenset
    """Defense-in-depth: even a cryptographically valid signature is rejected
    if the license ``type`` field is not in this set.  Prevents a compromised
    demo key from forging a ``commercial`` license."""

    description: str
    """Human-readable label shown in the Admin → License UI."""

    deprecated_after: Optional[date]
    """If set and today > deprecated_after, load succeeds but the admin UI
    displays a deprecation warning.  Does NOT cause rejection."""


# ---------------------------------------------------------------------------
# Bundled key material
# ---------------------------------------------------------------------------

# Existing Ed25519 key — 32 bytes hex-encoded, embedded since initial release.
# Generate a new keypair with: python tools/license/generate_keypair.py
_DEMO_LEGACY_RAW: bytes = bytes.fromhex(
    "e2b380f0d1c5205b119c96e7802165b55398c15f5b429e60c334a0e63315f23d"
)

# Commercial Ed25519 key introduced with the ipsolis-web shop (2026).
# Private half lives in the shop deployment as ``LICENSE_PRIVATE_KEY``.
_COMMERCIAL_2026_RAW: bytes = bytes.fromhex(
    "9afe5aaccc2a7e3b22148ad80a3d7128a80090edd448c105991cf1650a016338"
)

# ---------------------------------------------------------------------------
# Bundled trust list
# ---------------------------------------------------------------------------

TRUSTED_KEYS: list[TrustedKey] = [
    TrustedKey(
        key_id="demo-legacy",
        algorithm="ed25519",
        public_key_pem=None,
        public_key_raw=_DEMO_LEGACY_RAW,
        rsa_padding=None,
        rsa_hash=None,
        accepted_license_types=frozenset({"demo"}),
        description="ip·Solis demo licenses — Ed25519, issued by ipsolis-web",
        deprecated_after=None,
    ),
    TrustedKey(
        key_id="commercial-2026",
        algorithm="ed25519",
        public_key_pem=None,
        public_key_raw=_COMMERCIAL_2026_RAW,
        rsa_padding=None,
        rsa_hash=None,
        accepted_license_types=frozenset({"commercial"}),
        description="ip·Solis commercial licenses — Ed25519, issued by ipsolis-web shop",
        deprecated_after=None,
    ),
]

# O(1) lookup used by the hot verification path.  Rebuilt whenever
# operator_keys.py appends entries at startup.
TRUSTED_KEYS_BY_ID: dict[str, TrustedKey] = {k.key_id: k for k in TRUSTED_KEYS}
