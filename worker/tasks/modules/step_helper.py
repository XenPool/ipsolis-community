"""Shared helpers for order step tracking across all runbooks."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def make_log_json(
    module_key: str,
    input_params: dict,
    output: dict,
    duration_ms: float,
) -> str:
    """Creates a structured JSON log entry for a step.

    Format: {"module": key, "input": {...}, "output": {...}, "duration_ms": n}
    """

    def _sanitize(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, (int, float, str, bool, type(None))):
            return obj
        if isinstance(obj, list):
            return [_sanitize(i) for i in obj]
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        return str(obj)

    return json.dumps({
        "module": module_key,
        "input": _sanitize(input_params),
        "output": _sanitize(output),
        "duration_ms": round(duration_ms),
    })


def update_order_step(
    db: Session,
    order_id: int,
    step_name: str,
    status: str,
    log_output: str | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    """Inserts a step record into order_steps and commits."""
    db.execute(
        text("""
            INSERT INTO order_steps (order_id, step_name, status, started_at, finished_at, log_output, error)
            VALUES (:order_id, :step_name, :status, :started_at, :finished_at, :log_output, :error)
        """),
        {
            "order_id": order_id,
            "step_name": step_name,
            "status": status,
            "started_at": started_at or datetime.now(timezone.utc),
            "finished_at": finished_at,
            "log_output": log_output,
            "error": error,
        },
    )
    db.commit()


def update_order_status(
    db: Session,
    order_id: int,
    status: str,
    error: str | None = None,
) -> None:
    """Updates the order status (and optional error_message) and commits."""
    db.execute(
        text("UPDATE orders SET status = :status, error_message = :error, updated_at = NOW() WHERE id = :id"),
        {"status": status, "error": error, "id": order_id},
    )
    db.commit()
