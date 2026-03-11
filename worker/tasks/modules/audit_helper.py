"""Sync audit helper for Celery worker.

Writes audit entries via raw SQL (no ORM import from api/).
The caller is responsible for the commit.
"""

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def waudit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    *,
    old: dict | None = None,
    new: dict | None = None,
    by: str,
    ctx: str | None = None,
) -> None:
    """Writes an audit log entry (sync, no commit).

    Args:
        db:          Active SQLAlchemy Session (psycopg2)
        entity_type: "order" | "asset" | "asset_type" | "app_config"
        entity_id:   PK of the changed record
        action:      "created" | "status_changed" | "updated" | "deleted"
        old:         Snapshot before the change
        new:         Snapshot after the change
        by:          Trigger, e.g. "celery:vdi_provision"
        ctx:         Optional context (celery_task_id, etc.)
    """
    try:
        db.execute(
            text("""
                INSERT INTO audit_log
                  (entity_type, entity_id, action, old_value, new_value, triggered_by, context)
                VALUES
                  (:et, :eid, :act, CAST(:old AS JSON), CAST(:new AS JSON), :by, :ctx)
            """),
            {
                "et": entity_type,
                "eid": entity_id,
                "act": action,
                "old": json.dumps(old) if old is not None else None,
                "new": json.dumps(new) if new is not None else None,
                "by": by,
                "ctx": ctx,
            },
        )
    except Exception as e:
        # Audit errors must not interrupt the main runbook
        logger.error("waudit failed (non-critical): entity=%s:%s action=%s error=%s",
                     entity_type, entity_id, action, e)
