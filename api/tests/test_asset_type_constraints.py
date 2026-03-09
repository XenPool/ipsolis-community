"""Unit tests for AssetType constraint validation (5 core rules).

Run with:
    cd api && python -m pytest tests/test_asset_type_constraints.py -v

No DB or framework required – tests target the pure validate_asset_type() function.
"""

import pytest
from app.utils.asset_type_constraints import (
    ConstraintViolation,
    validate_asset_type,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _codes(violations: list[ConstraintViolation]) -> set[str]:
    return {v.code for v in violations}


def _valid(**kwargs) -> list[ConstraintViolation]:
    """Call validator with sensible defaults; caller overrides what they need."""
    defaults = dict(
        assignment_model="assigned_personal",
        automation_strategy="group_only",
        deprovision_policy="access_only",
        personal_provisioning_strategy=None,
        runbook_provision_id=None,
        runbook_revoke_id=None,
    )
    defaults.update(kwargs)
    return validate_asset_type(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
# MUST PASS cases
# ══════════════════════════════════════════════════════════════════════════════

class TestMustPass:
    def test_pool_group_only_access_only(self):
        """POOL (capacity_pooled) + GROUP_ONLY + ACCESS_ONLY → valid."""
        errors = _valid(
            assignment_model="capacity_pooled",
            automation_strategy="group_only",
            deprovision_policy="access_only",
        )
        assert errors == []

    def test_personal_runbook_only_stop_instance_with_provision_runbook(self):
        """PERSONAL + RUNBOOK_ONLY + STOP_INSTANCE + runbookProvisionId → valid."""
        errors = _valid(
            assignment_model="assigned_personal",
            automation_strategy="runbook_only",
            deprovision_policy="deallocate_instance",  # STOP_INSTANCE
            runbook_provision_id=42,
        )
        assert errors == []

    def test_personal_runbook_only_runbook_policy_with_both_runbooks(self):
        """PERSONAL + RUNBOOK_ONLY + RUNBOOK policy + both IDs → valid."""
        errors = _valid(
            assignment_model="assigned_personal",
            automation_strategy="runbook_only",
            deprovision_policy="custom_runbook",  # RUNBOOK policy
            runbook_provision_id=1,
            runbook_revoke_id=2,
        )
        assert errors == []

    def test_shared_composite_access_only(self):
        """SHARED + COMPOSITE + ACCESS_ONLY → valid."""
        errors = _valid(
            assignment_model="dedicated_shared",
            automation_strategy="composite",
            deprovision_policy="access_only",
        )
        assert errors == []

    def test_personal_composite_return_to_pool_assign_existing_free(self):
        """PERSONAL + COMPOSITE + RETURN_TO_POOL + ASSIGN_EXISTING_FREE → valid."""
        errors = _valid(
            assignment_model="assigned_personal",
            automation_strategy="composite",
            deprovision_policy="return_to_pool",
            personal_provisioning_strategy="assign_existing_free",
        )
        assert errors == []


# ══════════════════════════════════════════════════════════════════════════════
# MUST FAIL cases
# ══════════════════════════════════════════════════════════════════════════════

class TestMustFail:
    def test_group_only_stop_instance(self):
        """GROUP_ONLY + STOP_INSTANCE → Rule 1 violation."""
        errors = _valid(
            automation_strategy="group_only",
            deprovision_policy="deallocate_instance",
        )
        assert "FORBIDDEN_INSTANCE_POLICY_FOR_GROUP_ONLY" in _codes(errors)

    def test_group_only_delete_instance(self):
        """GROUP_ONLY + DELETE_INSTANCE → Rule 1 violation."""
        errors = _valid(
            automation_strategy="group_only",
            deprovision_policy="delete_instance",
        )
        assert "FORBIDDEN_INSTANCE_POLICY_FOR_GROUP_ONLY" in _codes(errors)

    def test_return_to_pool_with_non_personal_model(self):
        """RETURN_TO_POOL with assignmentModel != PERSONAL → Rule 2 violation."""
        errors = _valid(
            assignment_model="capacity_pooled",
            deprovision_policy="return_to_pool",
        )
        assert "RETURN_TO_POOL_REQUIRES_PERSONAL_ASSIGN_EXISTING_FREE" in _codes(errors)

    def test_return_to_pool_personal_but_wrong_strategy(self):
        """RETURN_TO_POOL + PERSONAL but strategy != ASSIGN_EXISTING_FREE → Rule 2 violation."""
        errors = _valid(
            assignment_model="assigned_personal",
            deprovision_policy="return_to_pool",
            personal_provisioning_strategy="create_new",
        )
        assert "RETURN_TO_POOL_REQUIRES_PERSONAL_ASSIGN_EXISTING_FREE" in _codes(errors)

    def test_shared_delete_instance(self):
        """SHARED + DELETE_INSTANCE → Rule 3 violation."""
        errors = _valid(
            assignment_model="dedicated_shared",
            automation_strategy="runbook_only",
            deprovision_policy="delete_instance",
            runbook_provision_id=5,
        )
        assert "SHARED_FORBIDS_DELETE_INSTANCE" in _codes(errors)

    def test_runbook_only_missing_provision_runbook(self):
        """RUNBOOK_ONLY missing runbookProvisionId → Rule 4 violation."""
        errors = _valid(
            automation_strategy="runbook_only",
            runbook_provision_id=None,
        )
        assert "RUNBOOK_ONLY_REQUIRES_PROVISION_RUNBOOK" in _codes(errors)

    def test_runbook_policy_missing_revoke_runbook(self):
        """deprovisionPolicy == RUNBOOK missing runbookRevokeId → Rule 4 violation."""
        errors = _valid(
            automation_strategy="runbook_only",
            deprovision_policy="custom_runbook",
            runbook_provision_id=1,    # provision runbook present
            runbook_revoke_id=None,    # revoke runbook missing
        )
        assert "RUNBOOK_POLICY_REQUIRES_REVOKE_RUNBOOK" in _codes(errors)


# ══════════════════════════════════════════════════════════════════════════════
# Rule 5 – COMPOSITE flexibility (explicit)
# ══════════════════════════════════════════════════════════════════════════════

class TestCompositeFlexibility:
    """COMPOSITE should allow any deprovision_policy (besides Rule 2/3 combos)."""

    def test_composite_access_only(self):
        errors = _valid(automation_strategy="composite", deprovision_policy="access_only")
        assert errors == []

    def test_composite_deallocate(self):
        errors = _valid(automation_strategy="composite", deprovision_policy="deallocate_instance")
        assert errors == []

    def test_composite_delete_personal(self):
        errors = _valid(
            assignment_model="assigned_personal",
            automation_strategy="composite",
            deprovision_policy="delete_instance",
        )
        assert errors == []

    def test_composite_custom_runbook_no_revoke_id_fails(self):
        """COMPOSITE + custom_runbook without revoke ID still triggers Rule 4."""
        errors = _valid(
            automation_strategy="composite",
            deprovision_policy="custom_runbook",
            runbook_revoke_id=None,
        )
        assert "RUNBOOK_POLICY_REQUIRES_REVOKE_RUNBOOK" in _codes(errors)


# ══════════════════════════════════════════════════════════════════════════════
# Multiple violations collected at once
# ══════════════════════════════════════════════════════════════════════════════

class TestMultipleViolations:
    def test_group_only_delete_and_runbook_policy_missing_id(self):
        """Two independent violations should both be reported."""
        errors = _valid(
            automation_strategy="group_only",
            deprovision_policy="delete_instance",
            runbook_revoke_id=None,
        )
        codes = _codes(errors)
        assert "FORBIDDEN_INSTANCE_POLICY_FOR_GROUP_ONLY" in codes
        # Rule 4 for revoke runbook only fires when deprovision_policy == custom_runbook,
        # not delete_instance, so only Rule 1 fires here.
        assert len(errors) == 1
