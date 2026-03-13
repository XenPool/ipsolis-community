"""Dynamic runbook executor – reads runbook definitions from the DB.

Replaces the hardcoded vdi_provision/modify/reclaim tasks as the central
dispatcher. Runbooks and steps are managed in the DB and can be adjusted
without Python changes or redeployment.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[mGKHFABCDJsu]")
from datetime import datetime, timezone

from celery import Task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session  # noqa: F401 – used by _run_step_inline/_run_targets_mode

from tasks import app
from tasks.modules import audit_helper
from tasks.modules.step_helper import make_log_json, update_order_step, update_order_status

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://xpuser:changeme@localhost:5432/itselfservice",
).replace("postgresql+asyncpg://", "postgresql+psycopg2://")


def _get_db_session() -> Session:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    return Session(engine)


def _run_step_inline(
    db: Session,
    order_id: int,
    step_name: str,
    fn,
    critical: bool,
) -> dict | None:
    """Executes a synthetic step and tracks order_steps.

    Returns result dict on success, None if a critical step failed.
    """
    update_order_step(db, order_id, step_name, "running", started_at=datetime.now(timezone.utc))
    t_start = time.monotonic()
    try:
        result = fn()
        duration_ms = (time.monotonic() - t_start) * 1000
        log_json = make_log_json(step_name, {}, result, duration_ms)

        if not result.get("success", True):
            raise RuntimeError(result.get("error", f"Step {step_name!r} returned success=False"))

        update_order_step(
            db, order_id, step_name, "success",
            log_output=log_json,
            finished_at=datetime.now(timezone.utc),
        )
        return result
    except Exception as e:
        duration_ms = (time.monotonic() - t_start) * 1000
        log_json = make_log_json(step_name, {}, {"error": str(e)}, duration_ms)
        update_order_step(
            db, order_id, step_name, "failed",
            log_output=log_json,
            error=str(e),
            finished_at=datetime.now(timezone.utc),
        )
        if critical:
            update_order_status(db, order_id, "failed", str(e))
            db.commit()
            logger.error("[targets_only] Critical step failed: %s – %s", step_name, e)
            return None
        else:
            logger.warning("[targets_only] Non-critical step failed (continuing): %s – %s", step_name, e)
            return {"success": False, "error": str(e)}


def _final_status(action: str) -> str:
    """Returns the final order status after successful execution."""
    if action == "provision":
        return "provisioned"
    if action == "delete":
        return "revoked"
    return "delivered"  # modify / extend


def _write_provisioned_state(
    db: Session,
    order_id: int,
    assignment_model: str,
    automation_strategy: str,
    deprovision_policy: str,
    asset_id=None,
    asset_name=None,
) -> None:
    """Writes provisioned_state after successful provision (deterministic revoke)."""
    state: dict = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "assignment_model": assignment_model,
        "automation_strategy": automation_strategy,
        "deprovision_policy": deprovision_policy,
        "lifecycle_status": "provisioned",
    }
    if asset_id is not None or asset_name is not None:
        state["instance_binding"] = {
            "asset_id": asset_id,
            "asset_name": asset_name,
        }
    db.execute(
        text("UPDATE orders SET provisioned_state = CAST(:state AS jsonb) WHERE id = :id"),
        {"state": json.dumps(state), "id": order_id},
    )
    logger.info("[dynamic_runner] provisioned_state written for order_id=%s", order_id)


def _stub_deallocate(order_id: int) -> dict:
    """Stub: Halt/deallocate VM. Real implementation via vsphere runbook."""
    logger.info("[STUB] Halt instance for order_id=%s – real implementation via runbook", order_id)
    return {"success": True, "stub": True, "message": "VM-Deallocate mocked (runbook implementation pending)"}


def _stub_delete_instance(order_id: int) -> dict:
    """Stub: Delete VM. Real implementation via vsphere runbook."""
    logger.info("[STUB] Delete instance for order_id=%s – real implementation via runbook", order_id)
    return {"success": True, "stub": True, "message": "VM-Delete mocked (runbook implementation pending)"}


def _run_targets_mode(
    celery_task,
    db: Session,
    order_id: int,
    order: dict,
    action: str,
    asset_type_name: str,
    asset_type_description: str,
    assignment_model: str,
    deprovision_policy: str = "access_only",
    automation_strategy: str = "group_only",
    _set_delivered: bool = True,
) -> dict:
    """Executes an order in group_only/targets_only automation mode.

    Provision: order confirmation → grant access → [reserve asset]
    Delete:    revoke access → deprovision_policy routing
    Extend:    no group changes, directly DELIVERED
    _set_delivered=False: DELIVERED status is not set (composite mode).
    """
    from tasks.modules import notifications as notif, pool_manager, target_executor

    logger.info(
        "=== targets_only START: order_id=%s action=%s assignment_model=%s ===",
        order_id, action, assignment_model,
    )

    expires_at = order["requested_until"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)

    requested_from = order["requested_from"]
    if isinstance(requested_from, str):
        requested_from = datetime.fromisoformat(requested_from)

    needs_asset = assignment_model in ("assigned_personal", "dedicated_shared")

    if action == "provision":
        # Step 1: Order confirmation (non-critical)
        _run_step_inline(
            db, order_id, "Order confirmation",
            lambda: notif.send_order_confirmation(
                db=db,
                user_email=order.get("user_email") or "",
                user_name=order.get("user_name") or "",
                owner_email=order.get("owner_email"),
                owner_name=order.get("owner_name"),
                asset_type_name=asset_type_name,
                asset_type_description=asset_type_description,
                requested_from=requested_from,
                requested_until=expires_at,
                snow_req=order.get("snow_req"),
                snow_ritm=order.get("servicenow_ref"),
            ),
            critical=False,
        )

        # Step 2: Grant access (critical)
        result = _run_step_inline(
            db, order_id, "Grant access",
            lambda: target_executor.grant(
                db=db,
                order_id=order_id,
                asset_type_id=order["asset_type_id"],
                user_email=order.get("user_email") or "",
                rdp_users=order.get("rdp_users") or [],
                admin_users=order.get("admin_users") or [],
            ),
            critical=True,
        )
        if result is None:
            audit_helper.waudit(
                db, "order", order_id, "status_changed",
                old={"status": "processing"},
                new={"status": "failed", "step": "Grant access"},
                by="celery:dynamic_runner[targets_only]",
                ctx=str(celery_task.request.id),
            )
            db.commit()
            return {"success": False, "order_id": order_id, "failed_step": "Grant access"}

        # Step 3: Asset reservieren (critical, nur bei assigned_personal/dedicated_shared)
        reserved_asset_id = None
        reserved_asset_name = None
        if needs_asset:
            result = _run_step_inline(
                db, order_id, "Reserve asset",
                lambda: pool_manager.reserve_asset(
                    db=db,
                    order_id=order_id,
                    asset_type_id=order["asset_type_id"],
                    expires_at=expires_at,
                    user_email=order.get("user_email"),
                ),
                critical=True,
            )
            if result is None:
                audit_helper.waudit(
                    db, "order", order_id, "status_changed",
                    old={"status": "processing"},
                    new={"status": "failed", "step": "Reserve asset"},
                    by="celery:dynamic_runner[targets_only]",
                    ctx=str(celery_task.request.id),
                )
                db.commit()
                return {"success": False, "order_id": order_id, "failed_step": "Reserve asset"}
            reserved_asset_id = result.get("asset_id")
            reserved_asset_name = result.get("asset_name")

        # Set asset to BUSY (pure DB op, no mock)
        if reserved_asset_id:
            pool_manager.set_asset_busy(db, reserved_asset_id, order_id, expires_at)

        # Write provisioned_state after successful provision
        _write_provisioned_state(
            db, order_id,
            assignment_model=assignment_model,
            automation_strategy=automation_strategy,
            deprovision_policy=deprovision_policy,
            asset_id=reserved_asset_id,
            asset_name=reserved_asset_name,
        )

    elif action == "delete":
        # Step 1: Revoke access (critical) – always, regardless of deprovision_policy
        result = _run_step_inline(
            db, order_id, "Revoke access",
            lambda: target_executor.revoke(
                db=db,
                user_email=order.get("user_email") or "",
                asset_type_id=order["asset_type_id"],
            ),
            critical=True,
        )
        if result is None:
            audit_helper.waudit(
                db, "order", order_id, "status_changed",
                old={"status": "processing"},
                new={"status": "failed", "step": "Revoke access"},
                by="celery:dynamic_runner[targets_only]",
                ctx=str(celery_task.request.id),
            )
            db.commit()
            return {"success": False, "order_id": order_id, "failed_step": "Revoke access"}

        # Step 2+: Policy-Routing
        asset_id = order.get("assigned_asset_id")

        if deprovision_policy == "access_only":
            # Targets revoked only – done (already handled above)
            pass

        elif deprovision_policy == "return_to_pool":
            # Release pool reservation (access already revoked above)
            if needs_asset and asset_id:
                _run_step_inline(
                    db, order_id, "Release assignment",
                    lambda: pool_manager.release_asset(db=db, asset_id=asset_id),
                    critical=False,
                )

        elif deprovision_policy == "deallocate_instance":
            # Revoke targets (above) + release pool + halt VM
            if needs_asset and asset_id:
                _run_step_inline(
                    db, order_id, "Release assignment",
                    lambda: pool_manager.release_asset(db=db, asset_id=asset_id),
                    critical=False,
                )
            _run_step_inline(
                db, order_id, "Pause instance",
                lambda: _stub_deallocate(order_id),
                critical=False,
            )

        elif deprovision_policy == "delete_instance":
            # Revoke targets (above) + release pool + delete VM
            if needs_asset and asset_id:
                _run_step_inline(
                    db, order_id, "Release assignment",
                    lambda: pool_manager.release_asset(db=db, asset_id=asset_id),
                    critical=False,
                )
            _run_step_inline(
                db, order_id, "Delete instance",
                lambda: _stub_delete_instance(order_id),
                critical=False,
            )

        elif deprovision_policy == "custom_runbook":
            # Targets revoked; VM cleanup via separate runbook
            logger.info(
                "[targets_only] deprovision_policy=custom_runbook: targets revoked, "
                "VM cleanup must be performed via runbook (order_id=%s)", order_id,
            )
        else:
            # Unknown policy: fallback to access_only (revoke targets only)
            logger.warning(
                "[targets_only] Unknown deprovision_policy=%r – fallback: access_only", deprovision_policy,
            )

    elif action == "extend":
        # TTL update only – no group change required
        logger.info("[targets_only] extend order_id=%s – no group changes needed", order_id)

    # Set final status (optional – in composite mode _run_composite_mode handles this)
    if _set_delivered:
        final = _final_status(action)
        update_order_status(db, order_id, final)
        audit_helper.waudit(
            db, "order", order_id, "status_changed",
            old={"status": "processing"},
            new={"status": final},
            by="celery:dynamic_runner[targets_only]",
            ctx=str(celery_task.request.id),
        )
        db.commit()
    logger.info("=== targets_only COMPLETE: order_id=%s ===", order_id)
    return {"success": True, "order_id": order_id}


def _load_global_vars(db: Session) -> dict:
    """Loads all active global_vars from DB as key→value dict."""
    rows = db.execute(text("SELECT key, value FROM global_vars ORDER BY key")).fetchall()
    return {row[0]: (row[1] or "") for row in rows}


def _build_ps_preamble(global_vars: dict, params: dict) -> str:
    """Builds PowerShell variable injection header.

    $VARS = @{ key = 'value'; ... }
    $PARAMS = @{ name = 'value'; ... }
    """
    def _ps_escape(v) -> str:
        if v is None:
            return "$null"
        s = str(v).replace("'", "''")
        return f"'{s}'"

    vars_pairs = "; ".join(f"{k} = {_ps_escape(v)}" for k, v in global_vars.items())
    params_pairs = "; ".join(f"{k} = {_ps_escape(v)}" for k, v in params.items())
    return f"$VARS = @{{ {vars_pairs} }}\n$PARAMS = @{{ {params_pairs} }}\n"


def _run_db_script(
    db: Session,
    script_module_id: int,
    rendered_params: dict,
) -> dict:
    """Executes a script_module from the DB.

    Writes to a temp file and calls pwsh/python/bash.
    Returns a dict with at minimum {"success": bool}.
    """
    # Load script content
    row = db.execute(
        text("SELECT name, script_content, script_type FROM script_modules WHERE id = :id"),
        {"id": script_module_id},
    ).fetchone()
    if not row:
        return {"success": False, "error": f"script_module {script_module_id} not found"}

    script_name, script_content, script_type = row[0], row[1], row[2]

    global_vars = _load_global_vars(db)
    preamble = _build_ps_preamble(global_vars, rendered_params)
    full_script = preamble + "\n" + script_content

    suffix = ".ps1" if script_type == "powershell" else (".py" if script_type == "python" else ".sh")
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
        tmp.write(full_script)
        tmp_path = tmp.name

    try:
        if script_type == "powershell":
            cmd = ["pwsh", "-NonInteractive", "-NoProfile", "-File", tmp_path]
        elif script_type == "python":
            cmd = ["python", tmp_path]
        else:
            cmd = ["bash", tmp_path]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        stdout_raw = _ANSI_ESCAPE.sub("", proc.stdout).strip()
        stderr_raw = _ANSI_ESCAPE.sub("", proc.stderr).strip()

        if proc.returncode != 0:
            return {
                "success": False,
                "module": script_name,
                "error": stderr_raw or f"Exit code {proc.returncode}",
                "stdout": stdout_raw,
                "stderr": stderr_raw,
            }

        stdout = stdout_raw
        try:
            result = json.loads(stdout)
            if "success" not in result:
                result["success"] = True
        except (json.JSONDecodeError, ValueError):
            result = {"success": True, "output": stdout}

        result["module"] = script_name
        result["stdout"] = stdout
        result["stderr"] = stderr_raw
        return result

    except subprocess.TimeoutExpired:
        return {"success": False, "module": script_name, "error": "Script timed out after 120s"}
    except Exception as e:
        return {"success": False, "module": script_name, "error": str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _render_params(params_template: dict, ctx: dict) -> dict:
    """Rendert params_template: {{key}} wird type-safe durch ctx[key] ersetzt."""
    rendered = {}
    for k, v in params_template.items():
        if isinstance(v, str) and v.startswith("{{") and v.endswith("}}"):
            key = v[2:-2].strip()
            rendered[k] = ctx.get(key)
        else:
            rendered[k] = v
    return rendered


def _run_runbook_path(
    celery_task,
    db: Session,
    order_id: int,
    order: dict,
    action: str,
    asset_type_name: str,
    asset_type_description: str,
    assignment_model: str = "assigned_personal",
    deprovision_policy: str = "access_only",
    automation_strategy: str = "runbook_only",
    personal_provisioning_strategy: str = "assign_existing_free",
    _set_delivered: bool = True,
) -> dict:
    """Executes the configured runbook for the asset type and action.

    Called directly by run() and by _run_composite_mode().
    _set_delivered=False: DELIVERED status is not set (composite mode).
    """
    # 1. Load runbook
    runbook_row = db.execute(
        text("""
            SELECT id, name, is_active
            FROM runbook_definitions
            WHERE asset_type_id = :at AND action = CAST(:ac AS order_action)
            LIMIT 1
        """),
        {"at": order["asset_type_id"], "ac": action},
    ).fetchone()

    if not runbook_row:
        if action in ("modify", "extend"):
            logger.info(
                "[runbook] No runbook defined for action=%s asset_type_id=%s — treating as success (no-op)",
                action, order["asset_type_id"],
            )
            update_order_status(db, order_id, "delivered", None)
            db.commit()
            return {"success": True, "skipped": True}
        err = f"No runbook found for asset_type_id={order['asset_type_id']} action={action}"
        logger.error(err)
        update_order_status(db, order_id, "failed", err)
        return {"success": False, "error": err}

    runbook_id, runbook_name, is_active = runbook_row
    if not is_active:
        err = f"Runbook '{runbook_name}' is disabled (is_active=False)"
        logger.error(err)
        update_order_status(db, order_id, "failed", err)
        return {"success": False, "error": err}

    # 2. Load steps
    step_rows = db.execute(
        text("""
            SELECT id, position, step_name, module_key, script_module_id,
                   params_template, is_critical, retry_count, timeout_seconds
            FROM runbook_steps
            WHERE runbook_id = :rid
            ORDER BY position
        """),
        {"rid": runbook_id},
    ).fetchall()

    if not step_rows:
        logger.warning("Runbook '%s' has no steps – order will be marked as delivered", runbook_name)
        if _set_delivered:
            update_order_status(db, order_id, _final_status(action))
        return {"success": True, "order_id": order_id}

    # 3. Execution-Kontext aufbauen
    expires_at = order["requested_until"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)

    requested_from = order["requested_from"]
    if isinstance(requested_from, str):
        requested_from = datetime.fromisoformat(requested_from)

    pre_asset_id = order.get("assigned_asset_id")
    pre_asset_name = None
    if pre_asset_id:
        ar = db.execute(
            text("SELECT name FROM asset_pool WHERE id = :id"),
            {"id": pre_asset_id},
        ).fetchone()
        if ar:
            pre_asset_name = ar[0]

    # Auto-reserve from pool for provision + pool-based asset types
    if (
        action == "provision"
        and assignment_model in ("assigned_personal", "dedicated_shared")
        and not pre_asset_id
    ):
        from tasks.modules.pool_manager import reserve_asset as _reserve_asset
        res = _reserve_asset(
            db,
            order_id=order_id,
            asset_type_id=order["asset_type_id"],
            expires_at=expires_at,
            personal_provisioning_strategy=personal_provisioning_strategy,
            user_email=order["user_email"],
        )
        if res.get("success"):
            pre_asset_id = res["asset_id"]
            pre_asset_name = res["asset_name"]
            logger.info(
                "[runbook_path] Auto-reserved asset: id=%s name=%s",
                pre_asset_id, pre_asset_name,
            )
        else:
            err = res.get("error", "No free asset available in pool")
            logger.error("[runbook_path] Auto-reserve failed: %s", err)
            update_order_status(db, order_id, "failed", err)
            return {"success": False, "error": err}

    ctx: dict = {
        "order_id": order_id,
        "asset_type_id": order["asset_type_id"],
        "asset_type_name": asset_type_name,
        "asset_type_description": asset_type_description,
        "user_email": order["user_email"],
        "user_name": order["user_name"],
        "owner_email": order.get("owner_email"),
        "owner_name": order.get("owner_name"),
        "rdp_users": order["rdp_users"] or [],
        "admin_users": order["admin_users"] or [],
        "requested_from": requested_from,
        "expires_at": expires_at,
        "asset_id": pre_asset_id,
        "asset_name": pre_asset_name,
        "snow_req": order.get("snow_req"),
        "snow_ritm": order.get("servicenow_ref"),
    }

    # 4. Execute steps
    for step_row in step_rows:
        step = step_row._asdict()
        step_name = step["step_name"]
        module_key = step["module_key"]
        script_module_id = step["script_module_id"]
        params_template = step["params_template"] or {}
        is_critical = step["is_critical"]

        step_ref = module_key or f"script_module:{script_module_id}"
        logger.info(
            "[runbook_path] Step pos=%s: %s (%s)",
            step["position"], step_name, step_ref,
        )
        update_order_step(
            db, order_id, step_name, "running",
            started_at=datetime.now(timezone.utc),
        )

        t_start = time.monotonic()
        try:
            rendered = _render_params(params_template, ctx)

            if script_module_id:
                # New path: execute DB script with global vars injected
                logger.debug("[runbook_path] script_module_id=%s params: %s", script_module_id, list(rendered.keys()))
                result = _run_db_script(db, script_module_id, rendered)
                log_json = make_log_json(step_ref, rendered, result, (time.monotonic() - t_start) * 1000)
            else:
                # Legacy path: Python module registry
                from tasks.modules.registry import MODULE_REGISTRY
                if module_key not in MODULE_REGISTRY:
                    raise RuntimeError(f"Unknown module: {module_key!r}")
                reg = MODULE_REGISTRY[module_key]
                fn = reg["fn"]
                needs_db = reg.get("needs_db", False)
                logger.debug("[runbook_path] %s params: %s", module_key, list(rendered.keys()))
                result = fn(db, **rendered) if needs_db else fn(**rendered)
                duration_ms = (time.monotonic() - t_start) * 1000
                for ok in reg.get("output_keys", []):
                    if ok in result:
                        ctx[ok] = result[ok]
                        logger.debug("[runbook_path] ctx[%s] = %s", ok, result[ok])
                log_json = make_log_json(module_key, rendered, result, duration_ms)

            if not result.get("success", True):
                raise RuntimeError(result.get("error", f"Module {step_ref} returned success=False"))

            update_order_step(
                db, order_id, step_name, "success",
                log_output=log_json,
                finished_at=datetime.now(timezone.utc),
            )

        except Exception as e:
            duration_ms = (time.monotonic() - t_start) * 1000
            log_json = make_log_json(
                step_ref, params_template, {"error": str(e)}, duration_ms
            )
            update_order_step(
                db, order_id, step_name, "failed",
                log_output=log_json,
                error=str(e),
                finished_at=datetime.now(timezone.utc),
            )
            if is_critical:
                update_order_status(db, order_id, "failed", str(e))
                audit_helper.waudit(
                    db, "order", order_id, "status_changed",
                    old={"status": "processing"},
                    new={"status": "failed", "error": str(e)},
                    by="celery:dynamic_runner",
                    ctx=str(celery_task.request.id),
                )
                db.commit()
                logger.error("[runbook_path] CRITICAL step failed: %s – %s", step_name, e)
                return {
                    "success": False,
                    "order_id": order_id,
                    "failed_step": step_name,
                    "error": str(e),
                }
            else:
                logger.warning(
                    "[runbook_path] Non-critical step failed (continuing): %s – %s",
                    step_name, e,
                )

    # provisioned_state nach erfolgreicher Provision schreiben
    if action == "provision":
        _write_provisioned_state(
            db, order_id,
            assignment_model=assignment_model,
            automation_strategy=automation_strategy,
            deprovision_policy=deprovision_policy,
            asset_id=ctx.get("asset_id"),
            asset_name=ctx.get("asset_name"),
        )

    # Set final status (optional – in composite mode _run_composite_mode handles this)
    if _set_delivered:
        final = _final_status(action)
        update_order_status(db, order_id, final)
        audit_helper.waudit(
            db, "order", order_id, "status_changed",
            old={"status": "processing"},
            new={"status": final},
            by="celery:dynamic_runner",
            ctx=str(celery_task.request.id),
        )
        db.commit()

    logger.info("=== runbook_path COMPLETE: order_id=%s asset=%s ===", order_id, ctx.get("asset_name"))
    return {
        "success": True,
        "order_id": order_id,
        "asset_name": ctx.get("asset_name"),
    }


def _run_composite_mode(
    celery_task,
    db: Session,
    order_id: int,
    order: dict,
    action: str,
    asset_type_name: str,
    asset_type_description: str,
    assignment_model: str,
    deprovision_policy: str = "access_only",
    composite_steps: list | None = None,
) -> dict:
    """Executes an order in COMPOSITE mode.

    Runs GROUP_TARGETS and RUNBOOK in the order configured via composite_steps.
    If a critical step fails, the sequence is aborted.

    composite_steps format: [{"type": "GROUP_TARGETS", "order": 1}, {"type": "RUNBOOK", "order": 2}]
    Default: groups first (order 1), runbook second (order 2).
    """
    steps = sorted(
        composite_steps or [
            {"type": "GROUP_TARGETS", "order": 1},
            {"type": "RUNBOOK", "order": 2},
        ],
        key=lambda s: s.get("order", 99),
    )

    logger.info(
        "=== composite START: order_id=%s action=%s steps=%s ===",
        order_id, action, [s.get("type") for s in steps],
    )

    for step in steps:
        step_type = step.get("type", "").upper()

        if step_type == "GROUP_TARGETS":
            result = _run_targets_mode(
                celery_task, db, order_id, order, action,
                asset_type_name, asset_type_description, assignment_model,
                deprovision_policy=deprovision_policy,
                automation_strategy="composite",
                _set_delivered=False,
            )
            if not result.get("success"):
                return result

        elif step_type == "RUNBOOK":
            result = _run_runbook_path(
                celery_task, db, order_id, order, action,
                asset_type_name, asset_type_description,
                assignment_model=assignment_model,
                deprovision_policy=deprovision_policy,
                automation_strategy="composite",
                _set_delivered=False,
            )
            if not result.get("success"):
                return result

        else:
            logger.warning("[composite] Unknown step_type=%r – skipped", step_type)

    # Alle Schritte erfolgreich – Status setzen
    final = _final_status(action)
    update_order_status(db, order_id, final)
    audit_helper.waudit(
        db, "order", order_id, "status_changed",
        old={"status": "processing"},
        new={"status": final},
        by="celery:dynamic_runner[composite]",
        ctx=str(celery_task.request.id),
    )
    db.commit()
    logger.info("=== composite COMPLETE: order_id=%s ===", order_id)
    return {"success": True, "order_id": order_id, "composite": True}


@app.task(
    name="tasks.workflows.dynamic_runner.run",
    bind=True,
    max_retries=0,
    queue="provision",
)
def run(self: Task, order_id: int) -> dict:
    """
    Dynamic runbook executor.

    Reads the matching runbook (asset_type_id + action) from the DB,
    renders the step params, and executes the modules from the registry.
    """
    logger.info("=== dynamic_runner START: order_id=%s ===", order_id)
    db = _get_db_session()

    try:
        # 1. Load order
        order_row = db.execute(
            text("""
                SELECT o.id, o.user_email, o.user_name, o.owner_email, o.owner_name,
                       o.asset_type_id, o.rdp_users, o.admin_users,
                       o.requested_from, o.requested_until, o.action,
                       o.servicenow_ref, o.snow_req, o.assigned_asset_id,
                       o.provisioned_state
                FROM orders o WHERE o.id = :id
            """),
            {"id": order_id},
        ).fetchone()

        if not order_row:
            err = f"Order {order_id} not found"
            logger.error(err)
            return {"success": False, "error": err}

        order = order_row._asdict()
        action = order["action"]
        if hasattr(action, "value"):
            action = action.value
        action = str(action).lower()

        # provisioned_state for deterministic revoke
        provisioned_state = order.get("provisioned_state") or {}

        # 1.5. Load asset type – determine automation strategy + deprovision policy
        at_row = db.execute(
            text("""
                SELECT name, description, automation_mode, assignment_model,
                       deprovision_policy, automation_strategy, composite_steps,
                       personal_provisioning_strategy
                FROM asset_types WHERE id = :id
            """),
            {"id": order["asset_type_id"]},
        ).fetchone()
        asset_type_name = at_row[0] if at_row else f"Type {order['asset_type_id']}"
        asset_type_description = at_row[1] if at_row else ""
        automation_mode = at_row[2] if at_row else "runbook"
        assignment_model = at_row[3] if at_row else "assigned_personal"
        deprovision_policy = at_row[4] if at_row else "access_only"
        automation_strategy = at_row[5] if at_row else None
        composite_steps = at_row[6] if at_row else None
        personal_provisioning_strategy = at_row[7] if at_row else "assign_existing_free"

        # Fallback: derive automation_strategy from automation_mode (legacy records)
        if not automation_strategy:
            automation_strategy = "group_only" if automation_mode == "targets_only" else "runbook_only"

        # For delete/revoke: read deprovision_policy + automation_strategy from provisioned_state
        # (deterministic – even if asset type was changed afterwards)
        if action == "delete" and provisioned_state:
            snap_policy = provisioned_state.get("deprovision_policy")
            snap_strategy = provisioned_state.get("automation_strategy")
            if snap_policy:
                logger.info(
                    "[dynamic_runner] deprovision_policy from snapshot: %s (current config: %s)",
                    snap_policy, deprovision_policy,
                )
                deprovision_policy = snap_policy
            if snap_strategy:
                logger.info(
                    "[dynamic_runner] automation_strategy from snapshot: %s (current config: %s)",
                    snap_strategy, automation_strategy,
                )
                automation_strategy = snap_strategy

        logger.info(
            "[dynamic_runner] automation_strategy=%s assignment_model=%s deprovision_policy=%s",
            automation_strategy, assignment_model, deprovision_policy,
        )

        # 2. Dispatch by automation_strategy
        if automation_strategy == "group_only":
            result = _run_targets_mode(
                self, db, order_id, order, action,
                asset_type_name, asset_type_description, assignment_model,
                deprovision_policy=deprovision_policy,
                automation_strategy=automation_strategy,
            )
        elif automation_strategy == "composite":
            result = _run_composite_mode(
                self, db, order_id, order, action,
                asset_type_name, asset_type_description, assignment_model,
                deprovision_policy=deprovision_policy,
                composite_steps=composite_steps,
            )
        else:
            # runbook_only: execute runbook
            result = _run_runbook_path(
                self, db, order_id, order, action,
                asset_type_name, asset_type_description,
                assignment_model=assignment_model,
                deprovision_policy=deprovision_policy,
                automation_strategy=automation_strategy,
                personal_provisioning_strategy=personal_provisioning_strategy,
            )

        # Post-DELETE: return asset to pool + revoke original PROVISION order
        if action == "delete" and result.get("success"):
            asset_id = order.get("assigned_asset_id")
            if asset_id:
                # Release asset (idempotent – _run_targets_mode may have already done it)
                try:
                    from tasks.modules.pool_manager import release_asset as _release_asset
                    _release_asset(db, asset_id)
                    logger.info("[dynamic_runner] Asset %s released after DELETE", asset_id)
                except Exception as _e:
                    logger.warning("[dynamic_runner] release_asset failed (non-critical): %s", _e)

                # Set original PROVISION order(s) to revoked
                try:
                    db.execute(
                        text("""
                            UPDATE orders
                            SET status = 'revoked'
                            WHERE assigned_asset_id = :aid
                              AND action = 'provision'
                              AND status IN ('delivered', 'provisioned')
                        """),
                        {"aid": asset_id},
                    )
                    db.commit()
                    logger.info(
                        "[dynamic_runner] PROVISION orders for asset %s set to revoked", asset_id
                    )
                except Exception as _e:
                    logger.warning(
                        "[dynamic_runner] provision order revoke failed (non-critical): %s", _e
                    )

        return result

    except Exception as e:
        logger.error(
            "=== dynamic_runner UNEXPECTED ERROR: order_id=%s error=%s ===",
            order_id, e,
        )
        try:
            update_order_status(db, order_id, "failed", str(e))
            db.commit()
        except Exception:
            pass
        return {"success": False, "order_id": order_id, "error": str(e)}
    finally:
        db.close()


@app.task(
    name="tasks.workflows.dynamic_runner.test_script_module",
    bind=True,
    queue="provision",
)
def test_script_module(self: Task, script_module_id: int, params: dict) -> dict:
    """Executes a DB script_module for the module editor test runner.

    Always returns a structured result dict – never raises.
    """
    db = _get_db_session()
    t_start = time.monotonic()
    try:
        result = _run_db_script(db, script_module_id, params)
        duration_ms = (time.monotonic() - t_start) * 1000
        return {
            "success": result.get("success", True),
            "output": result,
            "duration_ms": round(duration_ms),
        }
    except Exception as e:
        duration_ms = (time.monotonic() - t_start) * 1000
        return {
            "success": False,
            "script_module_id": script_module_id,
            "error": str(e),
            "duration_ms": round(duration_ms),
        }
    finally:
        db.close()
