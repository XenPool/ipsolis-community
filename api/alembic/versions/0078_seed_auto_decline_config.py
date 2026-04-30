"""Seed approval auto-decline config keys.

Auto-decline is the third lever in the approval-flow staleness story:

* ``approval.reminders_enabled`` (existing) — nudge approvers via email/Teams.
* ``approval.escalation_email`` (existing) — alert a contact after max reminders.
* ``approval.auto_decline_*`` (new, seeded here) — system-decline after N days
  of inactivity. Off by default so existing installs see no change until an
  admin opts in.

Once the threshold is reached the Beat task at
``tasks.workflows.approval_auto_decline.scan_and_auto_decline`` declines a
single pending approval per order and lets the existing veto-on-decline
semantics propagate to the order (status → rejected, requester emailed).

Revision ID: 0078
Revises: 0077
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0078"
down_revision: Union[str, None] = "0077"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KEYS = [
    (
        "approval.auto_decline_enabled",
        "false",
        "Master switch for auto-declining stale pending approvals (true/false). "
        "Off by default so existing installs are unchanged.",
    ),
    (
        "approval.auto_decline_after_days",
        "0",
        "Days a pending approval may sit before the system declines it. "
        "Counted from the approval row's created_at. 0 = disabled "
        "(equivalent to setting the master switch to false).",
    ),
    (
        "approval.auto_decline_message",
        "Auto-declined: no decision recorded within the configured "
        "inactivity window. Re-submit the request if access is still required.",
        "Decline reason recorded on the approval row + included in the "
        "rejection email sent to the requester.",
    ),
]


def upgrade() -> None:
    for key, value, description in _KEYS:
        op.execute(
            f"""
            INSERT INTO app_config (key, value, description, is_secret)
            VALUES ('{key}',
                    {_sql_literal(value)},
                    {_sql_literal(description)},
                    false)
            ON CONFLICT (key) DO NOTHING
            """
        )


def downgrade() -> None:
    for key, _, _ in _KEYS:
        op.execute(f"DELETE FROM app_config WHERE key = '{key}'")


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
