"""AWS Secrets Manager backend adapter — slice-2 enrichment.

Fourth backend in the external-secret-management feature alongside
Vault, CCP, and Azure KV. Adds the ``awssm://`` reference scheme
resolved via SigV4-signed calls to the Secrets Manager
``GetSecretValue`` API.

Reference grammar (resolved by ``app.utils.secrets``):

    awssm://<secret-name-or-id>            # returns SecretString as-is
    awssm://<secret-name-or-id>#<field>    # parses SecretString as JSON,
                                            # extracts the named field

The configured region (``secret.awssm.region``) governs which AWS
endpoint we hit. Cross-region references via explicit ARN are
queued for slice 2 polish.

Authentication: long-lived IAM access key + secret access key, with
an optional session token for STS-issued temporary credentials.
The IAM principal needs ``secretsmanager:GetSecretValue`` on the
secret(s) referenced; tighten via resource ARNs in the IAM policy.

Revision ID: 0085
Revises: 0084
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0085"
down_revision: Union[str, None] = "0084"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KEYS = [
    (
        "secret.awssm.region",
        "us-east-1",
        "AWS region hosting the Secrets Manager endpoint (e.g. 'eu-central-1', "
        "'us-east-1'). Determines the API host: secretsmanager.<region>.amazonaws.com.",
        False,
    ),
    (
        "secret.awssm.access_key_id",
        "",
        "IAM access key id for the SPN that has secretsmanager:GetSecretValue. "
        "Use STS-issued temporary credentials (with session token) where possible.",
        False,
    ),
    (
        "secret.awssm.secret_access_key",
        "",
        "IAM secret access key. Stored as a secret — masked in the admin UI.",
        True,
    ),
    (
        "secret.awssm.session_token",
        "",
        "Optional STS session token for temporary credentials (e.g. AssumeRole, "
        "instance profile). Leave blank for long-lived IAM user credentials.",
        True,
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

    # Refresh the backend description to enumerate the new option.
    op.execute(
        """
        UPDATE app_config
        SET description = 'External secret backend: ''db'' (default — plaintext in app_config), ''vault'' (HashiCorp Vault), ''ccp'' (CyberArk CCP/AIM), ''azurekv'' (Azure Key Vault), or ''awssm'' (AWS Secrets Manager).'
        WHERE key = 'secret.backend'
        """
    )


def downgrade() -> None:
    for key, _, _, _ in _KEYS:
        op.execute(f"DELETE FROM app_config WHERE key = {_lit(key)}")
    op.execute(
        """
        UPDATE app_config
        SET description = 'External secret backend: ''db'' (default — plaintext in app_config), ''vault'' (HashiCorp Vault), ''ccp'' (CyberArk CCP/AIM), or ''azurekv'' (Azure Key Vault).'
        WHERE key = 'secret.backend'
        """
    )


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
