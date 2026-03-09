"""Celery App – Einstiegspunkt für Worker, Beat und Flower."""

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
        "tasks.workflows.vdi_provision",
        "tasks.workflows.vdi_modify",
        "tasks.workflows.vdi_reclaim",
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
    task_acks_late=True,                     # Erst ACK nach erfolgreichem Abschluss
    worker_prefetch_multiplier=1,            # Keine Prefetch-Häufung bei langen Tasks
    task_routes={
        "tasks.workflows.vdi_provision.*": {"queue": "provision"},
        "tasks.workflows.vdi_modify.*": {"queue": "provision"},
        "tasks.workflows.vdi_reclaim.*": {"queue": "reclaim"},
        "tasks.workflows.dynamic_runner.*": {"queue": "provision"},
        "tasks.workflows.ps_module_installer.*": {"queue": "provision"},
        "tasks.modules.notifications.*": {"queue": "notifications"},
    },
    beat_schedule={
        # Stündlich ablaufende Assets prüfen + Erinnerungsmails senden
        "check-expiring-assets": {
            "task": "tasks.workflows.vdi_reclaim.check_expiring_assets",
            "schedule": crontab(minute=0),  # Jede volle Stunde
        },
    },
)
