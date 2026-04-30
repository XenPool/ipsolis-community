"""Add 'escalation as assignment' mode + the matching email template.

Today's escalation flow (slice 1, migration ``0059_approval_escalation``)
notifies the configured ``approval.escalation_email`` contact(s) via
email pointing at ``/ui/orders``. The contact reads the email, opens
the admin UI, and intervenes operationally — chase the approver,
reassign, or cancel. They don't actually *decide* on the approval —
they're a referee, not an approver.

This migration adds the *assignment* mode: when
``approval.escalation_assign = true``, the escalation flow instead
**creates new ``OrderApproval`` rows** for each escalation contact
(``approver_type = 'escalation'``) and emails them a tokenized
``/approve/<token>`` URL — same one-click decide path the original
approver had. The escalation contact can now approve / reject
directly from their inbox; no admin-UI login needed.

Default stays ``false`` so existing installs upgrade silently with
the slice-1 notify-only behaviour. Operators flip the switch in
*Settings → Compliance → Approval workflow* to opt in.

Email template ``approval_escalation_assigned`` is the assignment
counterpart to ``approval_escalated`` (notify-only). Same recipient
set (``approval.escalation_email``) but a different body — reflects
that the recipient is now the *new* approver, not just an
intervention contact.

Revision ID: 0092
Revises: 0091
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0092"
down_revision: Union[str, None] = "0091"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO app_config (key, value, description, is_secret, updated_at)
        VALUES (
            'approval.escalation_assign',
            'false',
            'Escalation behaviour. ''false'' (default) sends a notification email to '
            'approval.escalation_email pointing at the admin UI — the contact intervenes '
            'operationally but doesn''t decide. ''true'' creates new approval rows for '
            'each escalation contact (approver_type=''escalation'') and emails them a '
            'tokenized /approve/<token> URL so they can decide directly. Mode change '
            'applies to subsequent escalations only — already-escalated rows aren''t '
            'retroactively converted.',
            false,
            NOW()
        )
        ON CONFLICT (key) DO NOTHING
    """)

    op.execute("""
        INSERT INTO email_templates (event_key, description, subject, body, available_variables, is_active)
        VALUES (
            'approval_escalation_assigned',
            'Sent to approval.escalation_email contacts when an approval is escalated AND '
            'approval.escalation_assign=true. Contains a tokenized one-click approval URL '
            'so the recipient can approve/reject directly. Counterpart to '
            'approval_escalated (notify-only) — same recipients, different body.',
            '[{{company_name}}] Approval reassigned to you — {{asset_type_name}}',
            '<p>Hello,</p>
<p>An approval request has been reassigned to you after the original approver missed the response window.</p>
<p><strong>Original approver:</strong> {{approver_name}} &lt;{{approver_email}}&gt;<br>
<strong>Requester:</strong> {{requester_name}} &lt;{{requester_email}}&gt;<br>
<strong>Asset:</strong> {{asset_type_name}}<br>
<strong>Requested period:</strong> {{from_date}} – {{until_date}}</p>
<p>You can decide directly from this email:</p>
<p><a href="{{approval_url}}" style="background:#BB0A30;color:#fff;padding:8px 14px;text-decoration:none;border-radius:4px;font-weight:bold;">Review and decide →</a></p>
<p style="font-size:12px;color:#666;">The original approval request stays in the order''s history; this is a new step assigned to you. Either approving or declining counts as the decision for this branch — the order will resume processing per its existing approval rules.</p>',
            '["company_name","app_title","approver_name","approver_email","requester_name","requester_email","asset_type_name","from_date","until_date","approval_url"]',
            true
        )
        ON CONFLICT (event_key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM email_templates WHERE event_key = 'approval_escalation_assigned'")
    op.execute("DELETE FROM app_config WHERE key = 'approval.escalation_assign'")
