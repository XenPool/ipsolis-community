"""CyberArk Conjur backend adapter — slice-2 enrichment.

Fifth backend in the external-secret-management feature alongside
Vault, CCP, Azure KV, and AWS SM. Adds the ``conjur://`` reference
scheme resolved via Conjur's two-step host-API-key auth + secret
read flow.

Reference grammar (resolved by ``app.utils.secrets``):

    conjur://<identifier>           # variable kind (default), full secret value
    conjur://<identifier>#<field>   # parses value as JSON, extracts named field

Auth flow:
1. POST ``<conjur_url>/{account}/host/{host_id}/authn`` with the host's
   API key as the raw body and ``Accept-Encoding: base64``. Conjur
   returns the access token directly in the response body (Base64).
2. Cache the token (Conjur defaults to an 8-minute TTL; we cache for
   7 minutes to give a 1-minute safety margin).
3. GET ``<conjur_url>/secrets/{account}/variable/{identifier}`` with
   ``Authorization: Token token="<base64>"``. The response body is
   the secret value as plain text.

Identifier slashes are preserved end-to-end (URL-encoded per segment)
so ``prod/ipsolis/ad-bind-password`` works without contortions.

The ``account`` and ``host_id`` are baked into config rather than the
reference so a single ipSolis tenant talks to one Conjur principal.
That keeps references short and avoids leaking tenant structure into
every config row.

Revision ID: 0086
Revises: 0085
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0086"
down_revision: Union[str, None] = "0085"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KEYS = [
    (
        "secret.conjur.url",
        "",
        "Conjur API base URL — on-prem 'https://conjur.example.com' or "
        "Conjur Cloud 'https://<account>.secretsmgr.cyberark.cloud'. No "
        "trailing slash. Authentication paths are appended automatically.",
        False,
    ),
    (
        "secret.conjur.account",
        "",
        "Conjur account / organisation name — the first path segment in every "
        "Conjur API call. Often 'cyberark', 'default', or a tenant-specific "
        "name set during install.",
        False,
    ),
    (
        "secret.conjur.host_id",
        "",
        "Conjur host identity that authenticates ipSolis (e.g. 'host/ipsolis-prod' "
        "or just 'ipsolis-prod' — the 'host/' prefix is added automatically if "
        "missing). The host needs read permission on every variable referenced.",
        False,
    ),
    (
        "secret.conjur.api_key",
        "",
        "API key for the configured host. Stored as a secret — masked in the "
        "admin UI. Rotate from the Conjur side and paste the new value here.",
        True,
    ),
    (
        "secret.conjur.verify_tls",
        "true",
        "Verify the Conjur endpoint TLS cert. Set to 'false' for self-signed "
        "lab installs only — production should always verify.",
        False,
    ),
]


def upgrade() -> None:
    for key, value, description, is_secret in _KEYS:
        op.execute(
            f"""
            INSERT INTO app_config (key, value, description, is_secret)
            VALUES ({_lit(key)}, {_lit(value)}, {_lit(description)}, {str(is_secret).lower()})
            ON CONFLICT (key) DO NOTHING
            """
        )

    op.execute(
        """
        UPDATE app_config
        SET description = 'External secret backend: ''db'' (default — plaintext in app_config), ''vault'' (HashiCorp Vault), ''ccp'' (CyberArk CCP/AIM), ''azurekv'' (Azure Key Vault), ''awssm'' (AWS Secrets Manager), or ''conjur'' (CyberArk Conjur).'
        WHERE key = 'secret.backend'
        """
    )


def downgrade() -> None:
    for key, _, _, _ in _KEYS:
        op.execute(f"DELETE FROM app_config WHERE key = {_lit(key)}")
    op.execute(
        """
        UPDATE app_config
        SET description = 'External secret backend: ''db'' (default — plaintext in app_config), ''vault'' (HashiCorp Vault), ''ccp'' (CyberArk CCP/AIM), ''azurekv'' (Azure Key Vault), or ''awssm'' (AWS Secrets Manager).'
        WHERE key = 'secret.backend'
        """
    )


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
