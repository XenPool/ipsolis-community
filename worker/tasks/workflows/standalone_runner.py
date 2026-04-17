"""Standalone Runbook Runner – executes runbooks independent of asset types.

Provides:
- run(run_id)              : Execute a standalone runbook run
- check_cron_schedules()   : Beat task (every minute) to dispatch cron-scheduled runbooks
"""

import logging
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import text
from sqlalchemy.orm import Session

from tasks import app
from tasks.workflows.dynamic_runner import _load_global_vars, _run_db_script

logger = logging.getLogger(__name__)


def _get_sync_session() -> Session:
    """Creates a synchronous DB session for the worker."""
    import os
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as SyncSession
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@postgres:5432/xp_db",
    )
    engine = create_engine(db_url, pool_pre_ping=True)
    return SyncSession(engine)


def _render_params(params_template: dict | None, global_vars: dict) -> dict:
    """Render params_template: substitute {{key}} from global_vars, pass literals as-is."""
    if not params_template:
        return {}
    rendered = {}
    for k, v in params_template.items():
        if isinstance(v, str) and v.startswith("{{") and v.endswith("}}"):
            key = v[2:-2].strip()
            rendered[k] = global_vars.get(key, "")
        else:
            rendered[k] = v
    return rendered


@app.task(name="tasks.workflows.standalone_runner.run", bind=True, max_retries=0)
def run(self: Task, run_id: int) -> dict:
    """Execute a standalone runbook run."""
    db = _get_sync_session()
    try:
        return _execute_run(db, run_id)
    except Exception as exc:
        logger.exception("standalone_runner.run failed for run_id=%s", run_id)
        # Mark run as failed
        try:
            db.execute(
                text("""
                    UPDATE standalone_runbook_runs
                    SET status = 'failed', error_message = :err,
                        finished_at = :now
                    WHERE id = :id
                """),
                {"id": run_id, "err": str(exc), "now": datetime.now(timezone.utc)},
            )
            db.commit()
        except Exception:
            pass
        return {"success": False, "error": str(exc)}
    finally:
        db.close()


def _execute_run(db: Session, run_id: int) -> dict:
    """Core execution logic for a standalone runbook run."""
    now = datetime.now(timezone.utc)

    # Load run record
    run_row = db.execute(
        text("SELECT id, runbook_id, status FROM standalone_runbook_runs WHERE id = :id"),
        {"id": run_id},
    ).fetchone()
    if not run_row:
        raise RuntimeError(f"Run {run_id} not found")

    runbook_id = run_row[1]

    # Load runbook
    rb = db.execute(
        text("SELECT id, name, is_active FROM standalone_runbooks WHERE id = :id"),
        {"id": runbook_id},
    ).fetchone()
    if not rb:
        raise RuntimeError(f"Standalone runbook {runbook_id} not found")
    if not rb[2]:
        raise RuntimeError(f"Standalone runbook {runbook_id} is not active")

    # Load steps
    steps = db.execute(
        text("""
            SELECT id, position, step_name, script_module_id, params_template,
                   is_critical, retry_count, timeout_seconds
            FROM standalone_runbook_steps
            WHERE runbook_id = :rid
            ORDER BY position
        """),
        {"rid": runbook_id},
    ).fetchall()

    # Mark run as running
    db.execute(
        text("UPDATE standalone_runbook_runs SET status = 'running', started_at = :now WHERE id = :id"),
        {"id": run_id, "now": now},
    )
    db.commit()

    logger.info("standalone_runner: starting run %s for runbook '%s' (%d steps)",
                run_id, rb[1], len(steps))

    # Load global vars
    global_vars = _load_global_vars(db)

    all_ok = True
    for step_row in steps:
        step_id, position, step_name, script_module_id, params_template = (
            step_row[0], step_row[1], step_row[2], step_row[3], step_row[4],
        )
        is_critical, retry_count, timeout_seconds = step_row[5], step_row[6], step_row[7]

        step_start = datetime.now(timezone.utc)

        # Create run step record
        db.execute(
            text("""
                INSERT INTO standalone_runbook_run_steps
                    (run_id, step_name, position, status, started_at)
                VALUES (:run_id, :step_name, :position, 'running', :started_at)
            """),
            {"run_id": run_id, "step_name": step_name, "position": position, "started_at": step_start},
        )
        db.commit()

        # Get the run_step id
        run_step_row = db.execute(
            text("""
                SELECT id FROM standalone_runbook_run_steps
                WHERE run_id = :run_id AND position = :pos
                ORDER BY id DESC LIMIT 1
            """),
            {"run_id": run_id, "pos": position},
        ).fetchone()
        run_step_id = run_step_row[0] if run_step_row else None

        if not script_module_id:
            _update_run_step(db, run_step_id, "failed", error="No script module assigned")
            if is_critical:
                all_ok = False
                break
            continue

        # Render params
        rendered_params = _render_params(params_template or {}, global_vars)

        # Execute with retries
        result = None
        last_error = None
        attempts = max(1, retry_count)
        for attempt in range(1, attempts + 1):
            try:
                result = _run_db_script(db, script_module_id, rendered_params)
                if result.get("success"):
                    break
                last_error = result.get("error") or result.get("stderr") or "Step returned success=False"
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "standalone_runner: step '%s' attempt %d/%d failed: %s",
                    step_name, attempt, attempts, last_error,
                )

        step_end = datetime.now(timezone.utc)

        if result and result.get("success"):
            log_output = result.get("output") or result.get("stdout") or ""
            _update_run_step(db, run_step_id, "success", log_output=str(log_output), finished_at=step_end)
            logger.info("standalone_runner: step '%s' succeeded", step_name)
        else:
            _update_run_step(db, run_step_id, "failed", error=last_error, finished_at=step_end)
            logger.error("standalone_runner: step '%s' failed: %s", step_name, last_error)
            if is_critical:
                all_ok = False
                # Mark remaining steps as skipped
                _skip_remaining_steps(db, run_id, steps, position)
                break

    # Final status
    finished_at = datetime.now(timezone.utc)
    final_status = "success" if all_ok else "failed"
    error_msg = None if all_ok else "One or more critical steps failed"

    db.execute(
        text("""
            UPDATE standalone_runbook_runs
            SET status = :status, finished_at = :finished_at, error_message = :err
            WHERE id = :id
        """),
        {"id": run_id, "status": final_status, "finished_at": finished_at, "err": error_msg},
    )
    db.commit()

    logger.info("standalone_runner: run %s finished with status '%s'", run_id, final_status)
    return {"success": all_ok, "run_id": run_id, "status": final_status}


def _update_run_step(
    db: Session, run_step_id: int | None, status: str,
    log_output: str | None = None, error: str | None = None,
    finished_at: datetime | None = None,
) -> None:
    if not run_step_id:
        return
    if finished_at is None:
        finished_at = datetime.now(timezone.utc)
    db.execute(
        text("""
            UPDATE standalone_runbook_run_steps
            SET status = :status, log_output = :log, error = :err, finished_at = :finished
            WHERE id = :id
        """),
        {"id": run_step_id, "status": status, "log": log_output, "err": error, "finished": finished_at},
    )
    db.commit()


def _skip_remaining_steps(db: Session, run_id: int, steps: list, failed_position: int) -> None:
    """Mark steps after the failed position as skipped."""
    for step_row in steps:
        pos = step_row[1]
        if pos <= failed_position:
            continue
        db.execute(
            text("""
                INSERT INTO standalone_runbook_run_steps
                    (run_id, step_name, position, status)
                VALUES (:run_id, :step_name, :position, 'skipped')
            """),
            {"run_id": run_id, "step_name": step_row[2], "position": pos},
        )
    db.commit()


# ── Cron Dispatcher ───────────────────────────────────────────────────────────

@app.task(name="tasks.workflows.standalone_runner.check_cron_schedules")
def check_cron_schedules() -> dict:
    """Runs every minute via Beat. Checks if any standalone runbooks need to fire."""
    from croniter import croniter

    db = _get_sync_session()
    dispatched = 0
    try:
        now = datetime.now(timezone.utc)

        rows = db.execute(
            text("""
                SELECT id, name, cron_expression, skip_if_running
                FROM standalone_runbooks
                WHERE is_active = true AND cron_enabled = true AND cron_expression IS NOT NULL
            """)
        ).fetchall()

        for row in rows:
            rb_id, rb_name, cron_expr, skip_if_running = row[0], row[1], row[2], row[3]

            try:
                cron = croniter(cron_expr, now)
                prev_fire = cron.get_prev(datetime)
                # Check if the previous fire time is within the last 60 seconds
                diff = (now - prev_fire).total_seconds()
                if diff > 60:
                    continue
            except Exception as exc:
                logger.warning("Invalid cron expression for runbook %s ('%s'): %s", rb_id, cron_expr, exc)
                continue

            # Skip if already running
            if skip_if_running:
                active = db.execute(
                    text("""
                        SELECT COUNT(*) FROM standalone_runbook_runs
                        WHERE runbook_id = :rid AND status IN ('pending', 'running')
                    """),
                    {"rid": rb_id},
                ).fetchone()
                if active and active[0] > 0:
                    logger.debug("standalone_runner: skipping runbook %s (already running)", rb_name)
                    continue

            # Check we haven't already dispatched this minute
            recent = db.execute(
                text("""
                    SELECT COUNT(*) FROM standalone_runbook_runs
                    WHERE runbook_id = :rid AND trigger = 'scheduled'
                      AND created_at > :cutoff
                """),
                {"rid": rb_id, "cutoff": now.replace(second=0, microsecond=0)},
            ).fetchone()
            if recent and recent[0] > 0:
                continue

            # Create run and dispatch
            db.execute(
                text("""
                    INSERT INTO standalone_runbook_runs
                        (runbook_id, trigger, triggered_by, status, created_at)
                    VALUES (:rid, 'scheduled', 'celery_beat', 'pending', :now)
                """),
                {"rid": rb_id, "now": now},
            )
            db.commit()

            run_row = db.execute(
                text("""
                    SELECT id FROM standalone_runbook_runs
                    WHERE runbook_id = :rid ORDER BY id DESC LIMIT 1
                """),
                {"rid": rb_id},
            ).fetchone()

            if run_row:
                run.delay(run_row[0])
                dispatched += 1
                logger.info("standalone_runner: dispatched scheduled run for runbook '%s' (run_id=%s)",
                            rb_name, run_row[0])

    except Exception:
        logger.exception("standalone_runner: check_cron_schedules failed")
    finally:
        db.close()

    return {"dispatched": dispatched}
