"""Load operator-supplied trusted keys from /etc/ipsolis/trusted_keys.yaml.

Called once during application startup (after the bundled list is loaded).
Entries are *appended* to ``trusted_keys.TRUSTED_KEYS`` — additive only;
operators cannot remove or override bundled keys.

If the file is absent, unreadable, or PyYAML is not installed, a log message
is emitted and the bundled defaults continue to be the only trusted keys.

YAML schema (one entry per list item)::

    - key_id: my-internal-ca-2025
      algorithm: ed25519          # or: rsa
      description: "Internal CA for self-signed dev licenses"
      accepted_license_types:
        - demo
      # Ed25519 — provide public_key_hex (32 bytes):
      public_key_hex: "aabbcc..."
      # RSA — provide public_key_pem (PEM SPKI block):
      # public_key_pem: |
      #   -----BEGIN PUBLIC KEY-----
      #   ...
      #   -----END PUBLIC KEY-----
      # rsa_padding: pkcs1v15     # or: pss
      # rsa_hash: sha256          # or: sha512
      # deprecated_after: 2027-01-01
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from .trusted_keys import (
    TRUSTED_KEYS,
    TRUSTED_KEYS_BY_ID,
    TrustedKey,
)

logger = logging.getLogger(__name__)

OPERATOR_KEYS_PATH = Path("/etc/ipsolis/trusted_keys.yaml")


def _parse_entry(entry: dict) -> TrustedKey | None:
    try:
        key_id = str(entry["key_id"])
        algorithm = str(entry["algorithm"]).lower()
        if algorithm not in ("rsa", "ed25519"):
            logger.warning("operator_keys: unknown algorithm %r for key_id=%r — skipped", algorithm, key_id)
            return None

        description = str(entry.get("description", key_id))
        accepted_raw = entry.get("accepted_license_types", ["demo"])
        accepted = frozenset(str(t) for t in accepted_raw)

        dep_raw = entry.get("deprecated_after")
        deprecated_after = date.fromisoformat(str(dep_raw)) if dep_raw else None

        if algorithm == "ed25519":
            hex_key = entry.get("public_key_hex", "")
            raw = bytes.fromhex(str(hex_key))
            if len(raw) != 32:
                logger.warning("operator_keys: Ed25519 key_id=%r has wrong length — skipped", key_id)
                return None
            return TrustedKey(
                key_id=key_id,
                algorithm="ed25519",
                public_key_pem=None,
                public_key_raw=raw,
                rsa_padding=None,
                rsa_hash=None,
                accepted_license_types=accepted,
                description=description,
                deprecated_after=deprecated_after,
            )
        else:
            pem = str(entry.get("public_key_pem", "")).strip()
            if not pem:
                logger.warning("operator_keys: RSA key_id=%r missing public_key_pem — skipped", key_id)
                return None
            padding = entry.get("rsa_padding", "pkcs1v15")
            hash_alg = entry.get("rsa_hash", "sha256")
            return TrustedKey(
                key_id=key_id,
                algorithm="rsa",
                public_key_pem=pem + "\n",
                public_key_raw=None,
                rsa_padding=padding,  # type: ignore[arg-type]
                rsa_hash=hash_alg,    # type: ignore[arg-type]
                accepted_license_types=accepted,
                description=description,
                deprecated_after=deprecated_after,
            )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("operator_keys: failed to parse entry: %s — skipped", exc)
        return None


def load_operator_keys() -> None:
    """Parse the operator YAML file and append valid entries to the trust list.

    Silently skips if the file is absent.  Logs warnings for malformed entries
    but continues loading the rest.
    """
    if not OPERATOR_KEYS_PATH.exists():
        logger.debug("operator_keys: %s not found — using bundled defaults only", OPERATOR_KEYS_PATH)
        return

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "operator_keys: PyYAML not installed — cannot load %s. "
            "Install PyYAML or add it to requirements.txt.",
            OPERATOR_KEYS_PATH,
        )
        return

    try:
        raw = OPERATOR_KEYS_PATH.read_text(encoding="utf-8")
        entries = yaml.safe_load(raw)
    except Exception as exc:
        logger.warning("operator_keys: failed to read/parse %s: %s", OPERATOR_KEYS_PATH, exc)
        return

    if not isinstance(entries, list):
        logger.warning("operator_keys: %s must be a YAML list — ignored", OPERATOR_KEYS_PATH)
        return

    added = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key_id = str(entry.get("key_id", ""))
        if key_id in TRUSTED_KEYS_BY_ID:
            logger.warning("operator_keys: key_id=%r already exists in bundled list — skipped", key_id)
            continue
        trusted = _parse_entry(entry)
        if trusted:
            TRUSTED_KEYS.append(trusted)
            TRUSTED_KEYS_BY_ID[trusted.key_id] = trusted
            added += 1

    if added:
        logger.info("operator_keys: loaded %d additional trusted key(s) from %s", added, OPERATOR_KEYS_PATH)
