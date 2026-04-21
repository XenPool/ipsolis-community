"""Standalone Runbook Runner – executes runbooks independent of asset types.

Provides:
- run(run_id)              : Execute a standalone runbook run
- check_cron_schedules()   : Beat task (every minute) to dispatch cron-scheduled runbooks

Step variable sharing:
  PowerShell steps can set $global:varname = "value" (strings, arrays, hashtables).
  All $global: variables created during a step are automatically forwarded to
  subsequent steps.  This allows step 1 to export data that step 2+ can consume
  without any extra configuration.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import text
from sqlalchemy.orm import Session

from tasks import app
from tasks.workflows.dynamic_runner import (
    _load_global_vars,
    _has_param_block,
    _split_param_block,
    _build_ps_preamble,
    _build_ps_preamble_for_param_script,
    _build_ps_cli_args,
    _ANSI_ESCAPE,
)

logger = logging.getLogger(__name__)

# Markers for step variable export (must not collide with script output)
_EXPORT_START = "::__XP_STEP_EXPORTS_START__::"
_EXPORT_END = "::__XP_STEP_EXPORTS_END__::"


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


_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _render_params(
    params_template: dict | None,
    global_vars: dict,
    step_vars: dict | None = None,
) -> dict:
    """Render params_template: substitute {{key}} from step_vars (preferred) or global_vars.

    step_vars are PS `$global:` values exported by previous steps. They win over
    global_vars on a key collision so multi-step runbooks can thread per-run data
    through without having to persist it to the global_vars table. A leading
    `$global:` in the key is stripped to tolerate users copy-pasting PS syntax.
    Scalar dotted paths like `RecycleVM.name` are also resolved.

    Both whole-value (`"{{VMName}}"`) and inline (`"CN=VDI-{{VMName}},DC=..."`)
    bindings are supported. Missing keys resolve to an empty string.
    """
    if not params_template:
        return {}
    sv = step_vars or {}

    def _lookup(raw_key: str) -> str:
        key = raw_key.strip()
        if key.lower().startswith("$global:"):
            key = key[len("$global:"):]
        return _resolve_key(key, sv, global_vars)

    rendered = {}
    for k, v in params_template.items():
        if isinstance(v, str) and "{{" in v and "}}" in v:
            rendered[k] = _TEMPLATE_RE.sub(lambda m: _lookup(m.group(1)), v)
        else:
            rendered[k] = v
    return rendered


def _resolve_key(key: str, step_vars: dict, global_vars: dict) -> str:
    """Resolve a `{{key}}` against step_vars (first) then global_vars.

    Supports scalar dotted paths (e.g. `RecycleVM.name`) so a user can bind to a
    field on an exported PS object without having to export each field separately.
    """
    head, _, tail = key.partition(".")
    for source in (step_vars, global_vars):
        if head in source:
            value = source[head]
            if tail:
                for part in tail.split("."):
                    if isinstance(value, dict) and part in value:
                        value = value[part]
                    else:
                        return ""
            return "" if value is None else str(value)
    return ""


# ── Step variable helpers ────────────────────────────────────────────────────

def _ps_literal(value) -> str:
    """Convert a Python value to a PowerShell literal expression."""
    if value is None:
        return "$null"
    if isinstance(value, bool):
        return "$true" if value else "$false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, list):
        items = ", ".join(_ps_literal(v) for v in value)
        return f"@({items})"
    if isinstance(value, dict):
        pairs = "; ".join(f"'{k}' = {_ps_literal(v)}" for k, v in value.items())
        return "@{" + pairs + "}"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _build_step_vars_preamble(step_vars: dict) -> str:
    """Build PowerShell code that snapshots existing globals then injects step vars.

    Step var names are injected via Set-Variable rather than `$global:<name> =`
    because the epilogue of a previous step can legitimately export variables
    whose names contain characters that break dotted variable syntax — e.g.
    `Citrix.XenServer.Sessions`, where `$global:Citrix.XenServer.Sessions = ...`
    is parsed as property access on `$global:Citrix` and throws InvalidOperation.
    Set-Variable treats the name as an opaque string, which is what we want.
    """
    lines = [
        "# ── Step variable injection ──",
        "$__xpSnap = [System.Collections.Generic.HashSet[string]]::new()",
        "Get-Variable -Scope Global | ForEach-Object { [void]$__xpSnap.Add($_.Name) }",
    ]
    for k, v in step_vars.items():
        name_literal = "'" + k.replace("'", "''") + "'"
        lines.append(
            f"Set-Variable -Name {name_literal} -Value ({_ps_literal(v)}) "
            f"-Scope Global -Force -ErrorAction SilentlyContinue"
        )
    return "\n".join(lines) + "\n"


def _build_step_vars_epilogue() -> str:
    """Build PowerShell code that exports new/changed $global: vars as JSON."""
    return f"""
# ── Step variable export ──
$__xpExport = @{{}}
Get-Variable -Scope Global | Where-Object {{
    -not $__xpSnap.Contains($_.Name) -and $_.Name -notlike '__xp*'
}} | ForEach-Object {{
    try {{ $__xpExport[$_.Name] = $_.Value }} catch {{}}
}}
if ($__xpExport.Count -gt 0) {{
    Write-Output ""
    Write-Output "{_EXPORT_START}"
    Write-Output ($__xpExport | ConvertTo-Json -Depth 10 -Compress)
    Write-Output "{_EXPORT_END}"
}}
"""


def _parse_step_exports(stdout: str) -> tuple[str, dict]:
    """Extract step variable exports from stdout, return (clean_stdout, exports_dict)."""
    start_idx = stdout.find(_EXPORT_START)
    if start_idx == -1:
        return stdout, {}

    end_idx = stdout.find(_EXPORT_END, start_idx)
    if end_idx == -1:
        return stdout, {}

    json_str = stdout[start_idx + len(_EXPORT_START):end_idx].strip()
    clean = stdout[:start_idx].rstrip()

    try:
        exports = json.loads(json_str)
        if not isinstance(exports, dict):
            return clean, {}
        return clean, exports
    except (json.JSONDecodeError, ValueError):
        logger.warning("standalone_runner: failed to parse step exports JSON")
        return clean, {}


def _append_run_step_log(db: Session, run_step_id: int | None, chunk: str) -> None:
    """Append a chunk to standalone_runbook_run_steps.log_output and commit.

    Called live from the stdout reader so the Admin UI's auto-refresh view can
    show script output while the step is still executing (rather than only
    after the process exits, which blocks on long-running poll loops)."""
    if not run_step_id or not chunk:
        return
    try:
        db.execute(
            text(
                "UPDATE standalone_runbook_run_steps "
                "SET log_output = COALESCE(log_output, '') || :chunk "
                "WHERE id = :id"
            ),
            {"id": run_step_id, "chunk": chunk},
        )
        db.commit()
    except Exception:
        logger.exception("standalone_runner: live log append failed (run_step_id=%s)", run_step_id)
        try:
            db.rollback()
        except Exception:
            pass


def _run_script_with_step_vars(
    db: Session,
    script_module_id: int,
    rendered_params: dict,
    step_vars: dict,
    timeout: int = 120,
    run_step_id: int | None = None,
) -> tuple[dict, dict]:
    """Execute a script module with step variable injection and extraction.

    Streams stdout/stderr line-by-line, appending to
    ``standalone_runbook_run_steps.log_output`` every 0.5 s (or every 20 lines)
    so the live UI can tail the output while the step is running. Returns
    ``(result_dict, updated_step_vars)``.
    """
    row = db.execute(
        text("SELECT name, script_content, script_type FROM script_modules WHERE id = :id"),
        {"id": script_module_id},
    ).fetchone()
    if not row:
        return {"success": False, "error": f"script_module {script_module_id} not found"}, step_vars

    script_name, script_content, script_type = row[0], row[1], row[2]
    global_vars = _load_global_vars(db)

    is_ps = script_type == "powershell"
    step_vars_preamble = _build_step_vars_preamble(step_vars) if is_ps else ""
    step_vars_epilogue = _build_step_vars_epilogue() if is_ps else ""

    uses_param_block = is_ps and _has_param_block(script_content)
    cli_args: list[str] = []

    if is_ps and uses_param_block:
        param_block, rest = _split_param_block(script_content)
        preamble = _build_ps_preamble_for_param_script(global_vars, rendered_params)
        full_script = param_block + "\n" + step_vars_preamble + preamble + "\n" + rest + "\n" + step_vars_epilogue
        cli_args = _build_ps_cli_args(rendered_params)
    elif is_ps:
        preamble = _build_ps_preamble(global_vars, rendered_params)
        full_script = step_vars_preamble + preamble + "\n" + script_content + "\n" + step_vars_epilogue
    else:
        # Python / Bash – no step var injection (not requested)
        full_script = script_content

    suffix = ".ps1" if script_type == "powershell" else (".py" if script_type == "python" else ".sh")
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
        tmp.write(full_script)
        tmp_path = tmp.name

    try:
        if script_type == "powershell":
            cmd = ["pwsh", "-NoProfile", "-InputFormat", "None", "-File", tmp_path] + cli_args
        elif script_type == "python":
            cmd = ["python", "-u", tmp_path]
        else:
            cmd = ["bash", tmp_path]

        # Force line-buffered stdio when stdbuf is available so pwsh/Python
        # don't block-buffer into the pipe while a long poll is running.
        if shutil.which("stdbuf"):
            cmd = ["stdbuf", "-oL", "-eL"] + cmd

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            proc.stdin.write("Y\nY\nY\nY\nY\n")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        stdout_lines: list[str] = []
        pending: list[str] = []
        in_export_block = False
        last_flush = time.monotonic()
        FLUSH_SEC = 0.5
        FLUSH_LINES = 20
        deadline = time.monotonic() + timeout
        timed_out = False

        def _flush_pending() -> None:
            nonlocal last_flush
            if pending:
                _append_run_step_log(db, run_step_id, "\n".join(pending) + "\n")
                pending.clear()
            last_flush = time.monotonic()

        assert proc.stdout is not None
        while True:
            if time.monotonic() > deadline:
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break

            line = proc.stdout.readline()
            if line == "":
                if proc.poll() is not None:
                    break
                # no data yet; periodic flush then keep polling
                if pending and (time.monotonic() - last_flush) >= FLUSH_SEC:
                    _flush_pending()
                time.sleep(0.05)
                continue

            clean = _ANSI_ESCAPE.sub("", line.rstrip("\n"))
            stdout_lines.append(clean)

            stripped = clean.strip()
            if stripped == _EXPORT_START:
                in_export_block = True
                continue
            if stripped == _EXPORT_END:
                in_export_block = False
                continue
            if in_export_block:
                # swallow the export JSON — we'll re-parse it from the full buffer
                continue

            pending.append(clean)
            if len(pending) >= FLUSH_LINES or (time.monotonic() - last_flush) >= FLUSH_SEC:
                _flush_pending()

        # Drain any remaining buffered output after exit/kill
        try:
            tail = proc.stdout.read()
        except Exception:
            tail = ""
        if tail:
            for t_line in tail.splitlines():
                clean = _ANSI_ESCAPE.sub("", t_line)
                stdout_lines.append(clean)
                stripped = clean.strip()
                if stripped == _EXPORT_START:
                    in_export_block = True
                    continue
                if stripped == _EXPORT_END:
                    in_export_block = False
                    continue
                if in_export_block:
                    continue
                pending.append(clean)
        _flush_pending()

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()

        stdout_raw = "\n".join(stdout_lines).strip()

        # Extract step exports before processing result
        new_step_vars = dict(step_vars)
        if is_ps:
            stdout_raw, exports = _parse_step_exports(stdout_raw)
            if exports:
                new_step_vars.update(exports)
                logger.info("standalone_runner: step exported %d var(s): %s",
                            len(exports), ", ".join(exports.keys()))

        if timed_out:
            return {
                "success": False,
                "module": script_name,
                "error": f"Script timed out after {timeout}s",
                "stdout": stdout_raw,
                "stderr": "",
            }, new_step_vars

        returncode = proc.returncode or 0
        if returncode != 0:
            err = None
            if stdout_raw:
                # Try to extract error from the last JSON object the script printed
                for line in reversed(stdout_raw.splitlines()):
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            j = json.loads(line)
                            if isinstance(j, dict) and j.get("error"):
                                err = str(j["error"])
                                break
                        except (json.JSONDecodeError, ValueError):
                            pass
            return {
                "success": False,
                "module": script_name,
                "error": err or f"Exit code {returncode}",
                "stdout": stdout_raw,
                "stderr": "",
            }, new_step_vars

        try:
            result = json.loads(stdout_raw)
            if "success" not in result:
                result["success"] = True
        except (json.JSONDecodeError, ValueError):
            result = {"success": True, "output": stdout_raw}

        result["module"] = script_name
        result["stdout"] = stdout_raw
        result["stderr"] = ""
        return result, new_step_vars

    except Exception as e:
        return {"success": False, "module": script_name, "error": str(e)}, step_vars
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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
                   is_critical, retry_count, timeout_seconds, always_run
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

    # Shared step variables – populated by $global:varname in PowerShell steps
    step_vars: dict = {}

    all_ok = True
    run_failed = False
    first_failed_step: str | None = None

    for step_row in steps:
        step_id, position, step_name, script_module_id, params_template = (
            step_row[0], step_row[1], step_row[2], step_row[3], step_row[4],
        )
        is_critical, retry_count, timeout_seconds = step_row[5], step_row[6], step_row[7]
        always_run = step_row[8] if len(step_row) > 8 else False

        # After a critical-step failure, only `always_run` steps execute (the
        # rest get marked 'skipped' here so the DB reflects the final state).
        if run_failed and not always_run:
            db.execute(
                text("""
                    INSERT INTO standalone_runbook_run_steps
                        (run_id, step_name, position, status)
                    VALUES (:run_id, :step_name, :position, 'skipped')
                """),
                {"run_id": run_id, "step_name": step_name, "position": position},
            )
            db.commit()
            continue

        # Expose run state to `always_run` finalization steps via step_vars so
        # they can branch on whether the run is already in a failed state.
        step_vars["RunbookFailed"] = bool(run_failed)
        step_vars["RunbookFirstFailedStep"] = first_failed_step or ""

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
                run_failed = True
                first_failed_step = first_failed_step or step_name
            continue

        # Render params (step_vars override global_vars for the current run)
        rendered_params = _render_params(params_template or {}, global_vars, step_vars)

        # Execute with retries
        result = None
        last_error = None
        attempts = max(1, retry_count)
        for attempt in range(1, attempts + 1):
            try:
                result, step_vars = _run_script_with_step_vars(
                    db, script_module_id, rendered_params, step_vars,
                    timeout=timeout_seconds,
                    run_step_id=run_step_id,
                )
                if result.get("success"):
                    break
                last_error = result.get("error") or result.get("stderr") or "Step returned success=False"
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "standalone_runner: step '%s' attempt %d/%d failed: %s",
                    step_name, attempt, attempts, last_error,
                )

        # Persist $global:RunNotes exported by the step so the run row in the
        # list view carries a human-readable label (e.g. target hostname).
        # The value accumulates across steps — last writer wins.
        notes_val = step_vars.get("RunNotes")
        if notes_val is not None and str(notes_val) != "":
            db.execute(
                text("UPDATE standalone_runbook_runs SET notes = :n WHERE id = :id"),
                {"id": run_id, "n": str(notes_val)[:500]},
            )
            db.commit()

        step_end = datetime.now(timezone.utc)

        # log_output is already streamed into the row by _run_script_with_step_vars;
        # finalisation only sets status / error / finished_at so we don't clobber it.
        if result and result.get("success"):
            _finalize_run_step(db, run_step_id, "success", finished_at=step_end)
            logger.info("standalone_runner: step '%s' succeeded", step_name)
        else:
            _finalize_run_step(
                db, run_step_id, "failed",
                error=last_error, finished_at=step_end,
            )
            logger.error("standalone_runner: step '%s' failed: %s", step_name, last_error)
            if is_critical:
                all_ok = False
                run_failed = True
                first_failed_step = first_failed_step or step_name
                # Don't break: subsequent always_run steps still need to execute
                # (e.g. a finalisation step that sets asset status to 'Failed').

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


def _finalize_run_step(
    db: Session, run_step_id: int | None, status: str,
    error: str | None = None, finished_at: datetime | None = None,
) -> None:
    """Close out a step without touching ``log_output`` (which was already
    streamed in live by ``_append_run_step_log``)."""
    if not run_step_id:
        return
    if finished_at is None:
        finished_at = datetime.now(timezone.utc)
    db.execute(
        text("""
            UPDATE standalone_runbook_run_steps
            SET status = :status, error = :err, finished_at = :finished
            WHERE id = :id
        """),
        {"id": run_step_id, "status": status, "err": error, "finished": finished_at},
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
