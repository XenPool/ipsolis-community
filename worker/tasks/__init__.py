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
    ],
)

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
        "tasks.modules.notifications.*": {"queue": "notifications"},
    },
    beat_schedule={
        # Check hourly expiring assets + send reminder emails
        "check-expiring-assets": {
            "task": "tasks.workflows.dynamic_runner.check_expiring_assets",
            "schedule": crontab(minute=0),  # Every full hour
            "options": {"queue": "reclaim"},
        },
    },
)
