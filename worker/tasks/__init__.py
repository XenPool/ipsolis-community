"""Celery App – entry point for worker, beat, and flower."""

import os

from celery import Celery
from celery.schedules import crontab

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

app = Celery(
    "xp_worker",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=[
        "tasks.workflows.dynamic_runner",
        "tasks.workflows.ps_module_installer",
        "tasks.workflows.standalone_runner",
        "tasks.workflows.sccm_probe",
        "tasks.workflows.license_check",
        "tasks.workflows.siem_streamer",
        "tasks.workflows.approval_reminders",
        "tasks.modules.maintenance",
    ],
)

# OpenTelemetry tracing — opt-in via otel.* config keys. Must run before the
# Celery workers fork so the instrumentor wires into the task signals.
try:
    from tasks.tracing import setup_worker_tracing
    setup_worker_tracing()
except Exception:
    # Tracing setup failures must never block worker startup.
    import logging
    logging.getLogger(__name__).exception("Worker tracing setup failed")

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Berlin",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,                     # ACK only after successful completion
    worker_prefetch_multiplier=1,            # No prefetch accumulation for long-running tasks
    task_routes={
        "tasks.workflows.dynamic_runner.*": {"queue": "provision"},
        "tasks.workflows.ps_module_installer.*": {"queue": "provision"},
        "tasks.workflows.standalone_runner.*": {"queue": "provision"},
        "tasks.workflows.license_check.*": {"queue": "default"},
        "tasks.workflows.siem_streamer.*": {"queue": "default"},
        "tasks.workflows.approval_reminders.*": {"queue": "notifications"},
        "tasks.modules.notifications.*": {"queue": "notifications"},
        "tasks.modules.maintenance.*": {"queue": "default"},
    },
    beat_schedule={
        # Check hourly expiring assets + send reminder emails
        "check-expiring-assets": {
            "task": "tasks.workflows.dynamic_runner.check_expiring_assets",
            "schedule": crontab(minute=0),  # Every full hour
            "options": {"queue": "reclaim"},
        },
        # Dispatch scheduled orders whose start date has arrived
        "check-scheduled-orders": {
            "task": "tasks.workflows.dynamic_runner.check_scheduled_orders",
            "schedule": crontab(minute=0),  # Every full hour
            "options": {"queue": "provision"},
        },
        # Dispatch cron-scheduled standalone runbooks
        "dispatch-standalone-cron": {
            "task": "tasks.workflows.standalone_runner.check_cron_schedules",
            "schedule": crontab(minute="*"),  # Every minute
            "options": {"queue": "provision"},
        },
        # Scheduled database backups (cron-expression driven)
        "maintenance-backup-scheduler": {
            "task": "tasks.modules.maintenance.check_backup_schedule",
            "schedule": crontab(minute="*"),  # Every minute
            "options": {"queue": "default"},
        },
        # Health probe transitions → email alerts
        "maintenance-health-alert": {
            "task": "tasks.modules.maintenance.check_health_and_alert",
            "schedule": crontab(minute="*/5"),  # Every 5 minutes
            "options": {"queue": "default"},
        },
        # Daily license expiry check (30/14/7 day warnings + expired error)
        "license-expiry-check": {
            "task": "tasks.workflows.license_check.check_license_expiry",
            "schedule": crontab(hour=8, minute=0),  # Daily at 08:00 Europe/Berlin
            "options": {"queue": "default"},
        },
        # Stream new audit_log rows to the configured SIEM endpoint
        "siem-stream-audit-log": {
            "task": "tasks.workflows.siem_streamer.stream_audit_log",
            "schedule": crontab(minute="*"),  # Every minute
            "options": {"queue": "default"},
        },
        # Re-notify approvers who have not yet decided on stale requests
        "approval-reminder-scan": {
            "task": "tasks.workflows.approval_reminders.scan_and_remind",
            "schedule": crontab(minute=15),  # Hourly at :15 to spread Beat load
            "options": {"queue": "notifications"},
        },
    },
)
