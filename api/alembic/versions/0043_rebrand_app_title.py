"""Rebrand working titles to Ipsolis in live config.

Only updates rows still holding the old default values, so any install that
customized app.title or email.from_name via the Settings UI keeps its choice.

Revision ID: 0043
Revises: 0042
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE app_config
        SET value = 'Ipsolis', updated_at = NOW()
        WHERE key = 'app.title'
          AND value IN ('IT Selfservice', 'IT-Selfservice', 'IT-SelfService', 'XenPool IT Selfservice')
    """)
    op.execute("""
        UPDATE app_config
        SET value = 'Ipsolis', updated_at = NOW()
        WHERE key = 'email.from_name'
          AND value IN ('IT Selfservice', 'XenPool IT Selfservice')
    """)
    # Replace the old "IT Self-Service portal" phrase seeded into two email
    # template bodies (see migration 0016). Substring REPLACE is safe whether
    # or not the admin has edited the template; only the phrase itself changes.
    op.execute("""
        UPDATE email_templates
        SET body = REPLACE(body, 'IT Self-Service portal', 'Ipsolis portal'),
            updated_at = NOW()
        WHERE body LIKE '%IT Self-Service portal%'
    """)


def downgrade() -> None:
    # Rebrand is not reversible as data: the old default would only make sense
    # to a hypothetical out-of-project user. No-op.
    pass
