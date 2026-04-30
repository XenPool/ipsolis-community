"""Azure Key Vault secret-backend adapter — slice-2 enrichment.

Extends the existing external secret-management feature
(migration 0072 seeded Vault + CCP config keys; this slice adds
Azure Key Vault). Same flat ``secret.*`` config namespace, distinct
prefix per backend so a tenant can stage credentials for multiple
backends and switch the active one via ``secret.backend`` without
losing the inactive backend's settings.

Reference grammar (resolved by ``app.utils.secrets``):

    azurekv://<vault-name>/<secret-name>           # latest version
    azurekv://<vault-name>/<secret-name>?version=  # explicit version (rare)

Authentication uses Azure AD service-principal client_credentials —
the same MSAL flow already used for portal SSO, but with a separate
SPN. We deliberately don't reuse ``entra.client_id`` etc. because
the Key Vault SPN typically has a different role assignment from the
SSO SPN (SSO needs ``User.Read``, KV needs ``Key Vault Secrets User``
on the vault). Putting them in separate config keys lets ops give
each SPN its minimum-necessary access.

Revision ID: 0084
Revises: 0083
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0084"
down_revision: Union[str, None] = "0083"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KEYS = [
    (
        "secret.azurekv.tenant_id",
        "",
        "Azure AD tenant id (GUID) hosting the Key Vault SPN. May reuse the "
        "Entra ID SSO tenant or a separate one — they're independent on "
        "purpose so the KV SPN can carry a different role assignment.",
        False,
    ),
    (
        "secret.azurekv.client_id",
        "",
        "Application (client) id of the service principal that has "
        "'Key Vault Secrets User' (or equivalent) on the target vault(s).",
        False,
    ),
    (
        "secret.azurekv.client_secret",
        "",
        "Client secret for the Azure KV service principal. Stored encrypted "
        "via the existing app_config.is_secret flag — masked in the admin UI.",
        True,
    ),
    (
        "secret.azurekv.api_version",
        "7.4",
        "Azure Key Vault REST API version. Default 7.4 — matches the "
        "current GA version (Feb 2024) and is forward-compatible with 7.5+.",
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

    # Update the docstring on secret.backend to enumerate the new option.
    # This is purely cosmetic for the admin UI's "what does this mean?"
    # tooltip — the backend dispatch logic doesn't read the description.
    op.execute(
        """
        UPDATE app_config
        SET description = 'External secret backend: ''db'' (default — plaintext in app_config), ''vault'' (HashiCorp Vault), ''ccp'' (CyberArk CCP/AIM), or ''azurekv'' (Azure Key Vault).'
        WHERE key = 'secret.backend'
        """
    )


def downgrade() -> None:
    for key, _, _, _ in _KEYS:
        op.execute(f"DELETE FROM app_config WHERE key = {_lit(key)}")
    op.execute(
        """
        UPDATE app_config
        SET description = 'External secret backend: ''db'' (default — plaintext in app_config), ''vault'' (HashiCorp Vault), or ''ccp'' (CyberArk CCP/AIM).'
        WHERE key = 'secret.backend'
        """
    )


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
