"""Multi-key license signature verification.

``verify_license_payload`` is the single entry point for all signature checks.
It routes by ``key_id`` (present on new licenses) or iterates the trust list
(legacy licenses without a ``key_id`` field).

Never raises — all failures are returned as a ``VerificationResult`` with
``verified=False`` so the caller can emit useful log/UI messages.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .trusted_keys import TRUSTED_KEYS, TRUSTED_KEYS_BY_ID, TrustedKey

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    verified: bool
    key: Optional[TrustedKey]
    reason: str


def _canonical(payload: dict) -> bytes:
    """Deterministic serialization matching the ipsolis-web issuer.

    Python ``json.dumps(sort_keys=True, separators=(',',':'))`` byte-matches
    JavaScript ``JSON.stringify`` for ASCII-only values.  Non-ASCII licensee
    names are escaped to ``\\uXXXX`` by Python's default ``ensure_ascii=True``
    which may diverge from the Node.js issuer for non-ASCII content — flagged
    as an open item; all current issuers produce ASCII-safe payloads.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_ed25519(key: TrustedKey, message: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        pub = Ed25519PublicKey.from_public_bytes(key.public_key_raw)  # type: ignore[arg-type]
        pub.verify(signature, message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _verify_rsa(key: TrustedKey, message: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    try:
        pub = load_pem_public_key(key.public_key_pem.encode())  # type: ignore[union-attr]

        pad = (
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256() if key.rsa_hash == "sha256" else hashes.SHA512()),
                salt_length=asym_padding.PSS.MAX_LENGTH,
            )
            if key.rsa_padding == "pss"
            else asym_padding.PKCS1v15()
        )
        hash_alg = hashes.SHA256() if key.rsa_hash == "sha256" else hashes.SHA512()
        pub.verify(signature, message, pad, hash_alg)  # type: ignore[union-attr]
        return True
    except (InvalidSignature, ValueError, TypeError, Exception):
        return False


def _attempt(key: TrustedKey, message: bytes, signature: bytes) -> bool:
    if key.algorithm == "ed25519":
        return _verify_ed25519(key, message, signature)
    return _verify_rsa(key, message, signature)


def verify_license_payload(payload: dict, signature: bytes) -> VerificationResult:
    """Verify ``signature`` over ``payload`` (signature field already removed).

    Routing:
    - ``key_id`` present → single key lookup, no fallback.
    - ``key_id`` absent  → iterate keys accepting ``payload.get("type","demo")``.

    The ``type`` field in ``accepted_license_types`` is a defense-in-depth
    gate: a demo key cannot grant commercial features even if the signature
    happens to verify.
    """
    import base64

    message = _canonical(payload)
    license_type = payload.get("type", "demo")

    key_id = payload.get("key_id")

    # ── Routed path (key_id present) ────────────────────────────────────────
    if key_id is not None:
        key = TRUSTED_KEYS_BY_ID.get(str(key_id))
        if key is None:
            logger.warning("license: unknown key_id=%r — rejecting", key_id)
            return VerificationResult(verified=False, key=None, reason=f"unknown key_id: {key_id!r}")

        if not _attempt(key, message, signature):
            logger.warning("license: signature verification failed for key_id=%r", key_id)
            return VerificationResult(verified=False, key=key, reason="signature verification failed")

        if license_type not in key.accepted_license_types:
            logger.warning(
                "license: key_id=%r valid signature but type=%r not in accepted_license_types=%r",
                key_id, license_type, set(key.accepted_license_types),
            )
            return VerificationResult(
                verified=False,
                key=key,
                reason=f"signature valid but license type {license_type!r} not accepted by key {key_id!r}",
            )

        logger.info(
            "license: verified — key_id=%s type=%s expires=%s licensee=%s install=%s",
            key.key_id,
            license_type,
            payload.get("expires_at", "?"),
            payload.get("licensee", "?"),
            str(payload.get("install_uuid", ""))[:8] or "—",
        )
        return VerificationResult(verified=True, key=key, reason="ok")

    # ── Fallback path (legacy licenses without key_id) ───────────────────────
    candidates = [k for k in TRUSTED_KEYS if license_type in k.accepted_license_types]
    tried: list[str] = []

    for key in candidates:
        if _attempt(key, message, signature):
            if license_type not in key.accepted_license_types:
                # Shouldn't happen since we pre-filtered, but be defensive.
                continue
            logger.info(
                "license: verified (legacy) — key_id=%s type=%s expires=%s licensee=%s install=%s",
                key.key_id,
                license_type,
                payload.get("expires_at", "?"),
                payload.get("licensee", "?"),
                str(payload.get("install_uuid", ""))[:8] or "—",
            )
            return VerificationResult(verified=True, key=key, reason="ok")
        tried.append(key.key_id)

    logger.warning(
        "license: no trusted key verified the signature (type=%r, tried=%r)",
        license_type, tried,
    )
    return VerificationResult(
        verified=False,
        key=None,
        reason=f"no trusted key verified the signature (tried: {tried})",
    )
