"""Add email_templates table and email.from_name config key

Revision ID: 0016
Revises: 0015
Create Date: 2026-03-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # New config key: email.from_name
    conn.execute(sa.text("""
        INSERT INTO app_config (key, value, description, is_secret, created_at, updated_at)
        VALUES ('email.from_name', 'XenPool IT Selfservice',
                'Display name shown in the From field of outgoing emails', false, NOW(), NOW())
        ON CONFLICT (key) DO NOTHING
    """))

    # email_templates table
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS email_templates (
            id          SERIAL PRIMARY KEY,
            event_key   VARCHAR UNIQUE NOT NULL,
            description VARCHAR,
            subject     VARCHAR NOT NULL,
            body        TEXT NOT NULL,
            available_variables JSONB,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))

    # Seed default templates
    conn.execute(sa.text("""
        INSERT INTO email_templates (event_key, description, subject, body, available_variables, is_active)
        VALUES
        (
            'order_confirmation',
            'Sent to requester (and owner if different) when an order is submitted',
            '[{{company_name}}] Order confirmed – {{asset_type_name}}',
            '<p>Hello {{requester_name}},</p>
<p>your order has been successfully submitted and is now being processed.</p>
<table style="font-size:13px;border-collapse:collapse;">
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Type:</td><td style="padding:4px 0;font-weight:bold;">{{asset_type_name}}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Description:</td><td style="padding:4px 0;">{{asset_type_description}}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Period:</td><td style="padding:4px 0;">{{from_date}} – {{until_date}}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Requestor:</td><td style="padding:4px 0;">{{requester_name}} &lt;{{requester_email}}&gt;</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Owner:</td><td style="padding:4px 0;">{{owner_name}} &lt;{{owner_email}}&gt;</td></tr>
</table>
<p style="font-size:12px;color:#888;margin-top:16px;">You will receive another notification once the resource has been provisioned.</p>',
            '[{"name":"company_name"},{"name":"requester_name"},{"name":"requester_email"},{"name":"owner_name"},{"name":"owner_email"},{"name":"asset_type_name"},{"name":"asset_type_description"},{"name":"from_date"},{"name":"until_date"},{"name":"snow_req"},{"name":"snow_ritm"}]',
            true
        ),
        (
            'provision_confirmation',
            'Sent when the resource has been fully provisioned and is ready to use',
            '[{{company_name}}] Your access {{asset_name}} is ready',
            '<p>Hello {{requester_name}},</p>
<p>your resource has been successfully provisioned and is ready to use.</p>
<table style="font-size:13px;border-collapse:collapse;">
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Name:</td><td style="padding:4px 0;font-weight:bold;">{{asset_name}}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#555;">RDP Users:</td><td style="padding:4px 0;">{{rdp_users}}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Available until:</td><td style="padding:4px 0;">{{expires_at}}</td></tr>
</table>
<p>Please connect to <strong>{{asset_name}}</strong> using Remote Desktop.</p>',
            '[{"name":"company_name"},{"name":"requester_name"},{"name":"requester_email"},{"name":"asset_name"},{"name":"rdp_users"},{"name":"expires_at"}]',
            true
        ),
        (
            'expiry_reminder',
            'Sent when a resource is about to expire (configurable hours before expiry)',
            'Reminder: Your access {{asset_name}} expires in {{hours_remaining}}h',
            '<p>Hello {{requester_name}},</p>
<p>your resource is expiring soon.</p>
<table style="font-size:13px;border-collapse:collapse;">
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Name:</td><td style="padding:4px 0;font-weight:bold;">{{asset_name}}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Expires at:</td><td style="padding:4px 0;">{{expires_at}}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#555;">Remaining:</td><td style="padding:4px 0;">approx. {{hours_remaining}} hours</td></tr>
</table>
<p>If you need it longer, please extend the duration in the IT Self-Service portal before the expiry date.</p>',
            '[{"name":"company_name"},{"name":"requester_name"},{"name":"asset_name"},{"name":"expires_at"},{"name":"hours_remaining"}]',
            true
        ),
        (
            'reclaim_notification',
            'Sent when a resource is returned to the pool (after cancellation or expiry)',
            'Your access {{asset_name}} has been returned',
            '<p>Hello {{requester_name}},</p>
<p>your resource <strong>{{asset_name}}</strong> has been returned to the pool and is being reset.</p>
<p>If you need a new resource, feel free to place a new order in the IT Self-Service portal.</p>',
            '[{"name":"company_name"},{"name":"requester_name"},{"name":"asset_name"}]',
            true
        )
        ON CONFLICT (event_key) DO NOTHING
    """))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS email_templates"))
    conn.execute(sa.text("DELETE FROM app_config WHERE key = 'email.from_name'"))
