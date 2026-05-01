"""Catalog of every Beat-scheduled task.

Mirrors the ``beat_schedule`` dict in ``worker/tasks/__init__.py`` so
the admin UI can render a "what runs when" table without importing the
worker package (api ↔ worker stay decoupled).

Each entry:

* ``name``        — Beat schedule key (matches the worker dict key)
* ``task``        — fully-qualified Celery task path
* ``cadence``     — human-readable cron description (e.g. "Daily 03:15 Europe/Berlin")
* ``queue``       — Celery queue the task runs on
* ``description`` — what the task does and why it matters
* ``config_keys`` — list of ``app_config`` keys that influence this task
                    (or gate it off entirely). Empty list = no operator
                    knobs; the schedule is the only lever.
* ``community``   — True when this task is meaningful on a community-tier
                    install. Tasks gated behind Enterprise features are
                    flagged so the UI can render them as informational
                    only.

When you change the worker's ``beat_schedule`` dict, update this file too
— the api will silently drift otherwise. CI doesn't catch the
duplication; the value is documentation + UI surface, not enforcement.
"""
from __future__ import annotations

from typing import TypedDict


class BeatEntry(TypedDict):
    name: str
    task: str
    cadence: str
    queue: str
    description: str
    config_keys: list[str]
    community: bool


# Listed in the order operators most often want to find them — daily
# housekeeping near the top, more frequent operational ticks below.
BEAT_INVENTORY: list[BeatEntry] = [
    # ── Daily ────────────────────────────────────────────────────────────
    {
        "name": "cost-report-snapshot-daily",
        "task": "tasks.workflows.cost_report_snapshot.capture_daily_snapshot",
        "cadence": "Daily 02:00 Europe/Berlin",
        "queue": "default",
        "description": (
            "Snapshots the three cost-report views into "
            "``cost_report_snapshots`` so the report's *As of* date "
            "picker can render past dates accurately. Runs before the "
            "audit prune so the day's final state is captured."
        ),
        "config_keys": ["cost.snapshot_retention_days"],
        "community": True,
    },
    {
        "name": "audit-retention-prune",
        "task": "tasks.workflows.audit_retention.prune_old_rows",
        "cadence": "Daily 03:00 Europe/Berlin",
        "queue": "default",
        "description": (
            "Prunes ``audit_log`` rows past their retention window. "
            "Per-classification windows (PII / PHI / PCI) sit on top of "
            "the global default so regulated rows can be kept 7+ years "
            "while routine config changes drop after 90 days."
        ),
        "config_keys": [
            "retention.audit_log_days",
            "retention.pii_days",
            "retention.phi_days",
            "retention.pci_days",
        ],
        "community": True,
    },
    {
        "name": "api-token-purge-daily",
        "task": "tasks.workflows.api_token_purge.purge_old_tokens",
        "cadence": "Daily 03:15 Europe/Berlin",
        "queue": "default",
        "description": (
            "Hard-deletes revoked / expired API tokens older than "
            "``api_tokens.purge_after_days``. Opt-in (default 0 = "
            "disabled). Each deletion writes one ``api_token / "
            "hard_deleted`` audit row capturing name + prefix + reason."
        ),
        "config_keys": ["api_tokens.purge_after_days"],
        "community": False,
    },
    {
        "name": "approval-auto-decline-scan",
        "task": "tasks.workflows.approval_auto_decline.scan_and_auto_decline",
        "cadence": "Daily 03:30 Europe/Berlin",
        "queue": "notifications",
        "description": (
            "Declines pending approvals past the configured inactivity "
            "window. Off by default. At most one stale approval per "
            "order per tick; veto-on-decline propagates to the order."
        ),
        "config_keys": [
            "approval.auto_decline_enabled",
            "approval.auto_decline_after_days",
            "approval.auto_decline_message",
        ],
        "community": True,
    },
    {
        "name": "cost-threshold-alerter",
        "task": "tasks.workflows.cost_threshold_alerter.scan_and_alert",
        "cadence": "Daily 04:00 Europe/Berlin",
        "queue": "notifications",
        "description": (
            "Emails (and Teams-cards when enabled) on per-cost-center "
            "monthly-spend ceiling breaches. Hysteresis via "
            "``cost.threshold_alert_quiet_hours`` (default 24h) "
            "suppresses repeats."
        ),
        "config_keys": ["cost.threshold_alert_quiet_hours"],
        "community": True,
    },
    {
        "name": "certification-reminder-scan",
        "task": "tasks.workflows.certification_reminders.scan_and_remind",
        "cadence": "Daily 04:30 Europe/Berlin",
        "queue": "notifications",
        "description": (
            "Certification-campaign reminders + overdue email + "
            "escalation summary + opt-in auto-revoke on overdue. Each "
            "lever gated on its own config flag."
        ),
        "config_keys": [
            "certification.reminder_offsets",
            "certification.escalation_email",
            "certification.auto_revoke_on_overdue",
        ],
        "community": False,
    },
    {
        "name": "update-notifier-daily",
        "task": "tasks.workflows.update_checker.check_for_updates",
        "cadence": "Daily 04:30 Europe/Berlin",
        "queue": "default",
        "description": (
            "Checks for newer ip·Solis releases. Opt-in via "
            "``updates.check_enabled``. Short-circuits to no-op when "
            "disabled — cheap on installs that don't use it."
        ),
        "config_keys": ["updates.check_enabled", "updates.github_token"],
        "community": True,
    },
    {
        "name": "license-expiry-check",
        "task": "tasks.workflows.license_check.check_license_expiry",
        "cadence": "Daily 08:00 Europe/Berlin",
        "queue": "default",
        "description": (
            "Daily license-expiry probe. Emails warnings at 30 / 14 / "
            "7 days before expiry, plus an error after expiry."
        ),
        "config_keys": ["license.warning_email"],
        "community": True,
    },

    # ── Hourly ──────────────────────────────────────────────────────────
    {
        "name": "check-expiring-assets",
        "task": "tasks.workflows.dynamic_runner.check_expiring_assets",
        "cadence": "Hourly at :00",
        "queue": "reclaim",
        "description": (
            "Sends reminder emails for assets whose lease is about to "
            "expire and reclaims any past their ``requested_until``. "
            "Reclaim policy depends on the asset type's "
            "``deprovision_policy``."
        ),
        "config_keys": [],
        "community": True,
    },
    {
        "name": "check-scheduled-orders",
        "task": "tasks.workflows.dynamic_runner.check_scheduled_orders",
        "cadence": "Hourly at :00",
        "queue": "provision",
        "description": (
            "Dispatches scheduled (future-dated) orders whose start "
            "date has arrived. Reserved assets go into the configured "
            "provision runbook."
        ),
        "config_keys": [],
        "community": True,
    },
    {
        "name": "approval-reminder-scan",
        "task": "tasks.workflows.approval_reminders.scan_and_remind",
        "cadence": "Hourly at :15",
        "queue": "notifications",
        "description": (
            "Re-notifies approvers who haven't decided. Stops after "
            "``approval.max_reminders`` nudges. Optional escalation "
            "(notify-only or assignment mode) when reminders exhaust."
        ),
        "config_keys": [
            "approval.reminders_enabled",
            "approval.reminder_after_hours",
            "approval.max_reminders",
            "approval.escalation_email",
            "approval.escalation_assign",
        ],
        "community": True,
    },

    # ── Every 5 minutes ─────────────────────────────────────────────────
    {
        "name": "maintenance-health-alert",
        "task": "tasks.modules.maintenance.check_health_and_alert",
        "cadence": "Every 5 minutes",
        "queue": "default",
        "description": (
            "Compares health-probe results against the previous tick "
            "and emails on OK→FAILED (or recovery) transitions. "
            "Cooldown window suppresses repeat alerts during outages."
        ),
        "config_keys": [
            "maintenance.alert_enabled",
            "maintenance.alert_email",
            "maintenance.alert_cooldown_minutes",
        ],
        "community": True,
    },

    # ── Every minute ────────────────────────────────────────────────────
    {
        "name": "dispatch-standalone-cron",
        "task": "tasks.workflows.standalone_runner.check_cron_schedules",
        "cadence": "Every minute",
        "queue": "provision",
        "description": (
            "Walks ``standalone_runbooks`` rows whose cron expression "
            "is due and dispatches them. Per-runbook ``cron_enabled`` "
            "gate skips disabled rows cheaply."
        ),
        "config_keys": [],
        "community": False,
    },
    {
        "name": "maintenance-backup-scheduler",
        "task": "tasks.modules.maintenance.check_backup_schedule",
        "cadence": "Every minute",
        "queue": "default",
        "description": (
            "Checks the backup schedule (cron expression in "
            "``maintenance.schedule_cron``) and triggers a "
            "``pg_dump`` when due. Retention policy enforced after."
        ),
        "config_keys": [
            "maintenance.schedule_enabled",
            "maintenance.schedule_cron",
            "maintenance.backup_retention_count",
        ],
        "community": True,
    },
    {
        "name": "siem-stream-audit-log",
        "task": "tasks.workflows.siem_streamer.stream_audit_log",
        "cadence": "Every minute",
        "queue": "default",
        "description": (
            "Forwards new ``audit_log`` rows to the configured SIEM "
            "(Splunk HEC / Sentinel Data Collector / Sentinel Logs "
            "Ingestion / generic webhook). Cursor-based — at-least-"
            "once delivery with persistent ``siem.last_id``."
        ),
        "config_keys": [
            "siem.enabled",
            "siem.format",
            "siem.batch_size",
        ],
        "community": False,
    },
]
