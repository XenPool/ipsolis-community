"""Use {{app_title}} instead of {{company_name}} in email template subjects.

Rewrites existing email_templates rows so subject prefixes and salutations pick
up the Application Title from Settings. The {{company_name}} variable is still
supplied for backwards compatibility in admin-customised templates.

Revision ID: 0044
Revises: 0043
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE email_templates
        SET subject = REPLACE(subject, '{{company_name}}', '{{app_title}}'),
            body    = REPLACE(body,    '{{company_name}}', '{{app_title}}')
        WHERE subject LIKE '%{{company_name}}%'
           OR body    LIKE '%{{company_name}}%'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE email_templates
        SET subject = REPLACE(subject, '{{app_title}}', '{{company_name}}'),
            body    = REPLACE(body,    '{{app_title}}', '{{company_name}}')
        WHERE subject LIKE '%{{app_title}}%'
           OR body    LIKE '%{{app_title}}%'
    """)
