"""AssetType Constraint Validation – 5 core rules.

Pure function, no DB access. Called by create/update route handlers.

Mapping (spec → codebase string values):
    GROUP_ONLY     → "group_only"
    RUNBOOK_ONLY   → "runbook_only"
    COMPOSITE      → "composite"
    PERSONAL       → "assigned_personal"
    SHARED         → "dedicated_shared"
    RETURN_TO_POOL → "return_to_pool"
    STOP_INSTANCE  → "deallocate_instance"
    DELETE_INSTANCE→ "delete_instance"
    RUNBOOK (policy) → "custom_runbook"
    ACCESS_ONLY    → "access_only"
    ASSIGN_EXISTING_FREE → "assign_existing_free"
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ConstraintViolation:
    code: str
    message: str


# ── Internal constants ─────────────────────────────────────────────────────────

_GROUP_ONLY    = "group_only"
_RUNBOOK_ONLY  = "runbook_only"

_PERSONAL = "assigned_personal"
_SHARED   = "dedicated_shared"

_RETURN_TO_POOL     = "return_to_pool"
_DEALLOCATE         = "deallocate_instance"   # spec: STOP_INSTANCE
_DELETE_INSTANCE    = "delete_instance"
_CUSTOM_RUNBOOK     = "custom_runbook"        # spec: RUNBOOK policy

_ASSIGN_EXISTING_FREE = "assign_existing_free"

_INSTANCE_LIFECYCLE_POLICIES = {_DEALLOCATE, _DELETE_INSTANCE}


# ── Public validator ───────────────────────────────────────────────────────────

def validate_asset_type(
    *,
    assignment_model: str,
    automation_strategy: str,
    deprovision_policy: str,
    personal_provisioning_strategy: str | None,
    runbook_provision_id: int | None,
    runbook_revoke_id: int | None,
) -> list[ConstraintViolation]:
    """Validate an AssetType payload against the 5 core constraint rules.

    Returns a (possibly empty) list of ConstraintViolation objects.
    An empty list means the payload is valid.

    All errors are collected before returning so callers can surface all
    problems at once instead of one at a time.
    """
    errors: list[ConstraintViolation] = []

    # ── Derived flag ───────────────────────────────────────────────────────────
    # Rule 1 basis: GROUP_ONLY has no instance lifecycle support.
    supports_instance_lifecycle = (automation_strategy != _GROUP_ONLY)

    # ── Rule 1 – Derived flag: supportsInstanceLifecycle ──────────────────────
    # If supportsInstanceLifecycle == False, forbid STOP_INSTANCE and DELETE_INSTANCE.
    if not supports_instance_lifecycle:
        if deprovision_policy in _INSTANCE_LIFECYCLE_POLICIES:
            errors.append(ConstraintViolation(
                code="FORBIDDEN_INSTANCE_POLICY_FOR_GROUP_ONLY",
                message=(
                    f"deprovision_policy='{deprovision_policy}' requires instance lifecycle, "
                    f"which is not supported by automation_strategy='{automation_strategy}'. "
                    f"Allowed policies for group_only: "
                    f"'access_only', 'return_to_pool', 'custom_runbook'."
                ),
            ))

    # ── Rule 2 – RETURN_TO_POOL requires PERSONAL + ASSIGN_EXISTING_FREE ──────
    if deprovision_policy == _RETURN_TO_POOL:
        if assignment_model != _PERSONAL:
            errors.append(ConstraintViolation(
                code="RETURN_TO_POOL_REQUIRES_PERSONAL_ASSIGN_EXISTING_FREE",
                message=(
                    f"deprovision_policy='return_to_pool' requires "
                    f"assignment_model='assigned_personal', got '{assignment_model}'."
                ),
            ))
        elif personal_provisioning_strategy != _ASSIGN_EXISTING_FREE:
            errors.append(ConstraintViolation(
                code="RETURN_TO_POOL_REQUIRES_PERSONAL_ASSIGN_EXISTING_FREE",
                message=(
                    f"deprovision_policy='return_to_pool' requires "
                    f"personal_provisioning_strategy='assign_existing_free', "
                    f"got '{personal_provisioning_strategy}'."
                ),
            ))

    # ── Rule 3 – Shared asset protection ──────────────────────────────────────
    # Shared (dedicated_shared) assets must not be deleted on revoke.
    if assignment_model == _SHARED and deprovision_policy == _DELETE_INSTANCE:
        errors.append(ConstraintViolation(
            code="SHARED_FORBIDS_DELETE_INSTANCE",
            message=(
                "assignment_model='dedicated_shared' forbids "
                "deprovision_policy='delete_instance'. "
                "Shared assets must not be deleted on revoke."
            ),
        ))

    # ── Rule 4 – Runbook requirements ─────────────────────────────────────────
    if automation_strategy == _RUNBOOK_ONLY and not runbook_provision_id:
        errors.append(ConstraintViolation(
            code="RUNBOOK_ONLY_REQUIRES_PROVISION_RUNBOOK",
            message="automation_strategy='runbook_only' requires runbook_provision_id to be set.",
        ))

    if deprovision_policy == _CUSTOM_RUNBOOK and not runbook_revoke_id:
        errors.append(ConstraintViolation(
            code="RUNBOOK_POLICY_REQUIRES_REVOKE_RUNBOOK",
            message="deprovision_policy='custom_runbook' requires runbook_revoke_id to be set.",
        ))

    # ── Rule 5 – COMPOSITE flexibility ────────────────────────────────────────
    # COMPOSITE allows all deprovision_policy values. No additional restrictions
    # beyond Rules 2 and 3 which already apply unconditionally above.

    return errors
