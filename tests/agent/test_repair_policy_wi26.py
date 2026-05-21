"""Tests for WI-26: L3 locator repair around uniqueness + evidence.

Pre-WI-26 the L3 deterministic scorer mapped similarity to a confidence
band via fixed thresholds (0.85 / 0.65 / 0.55). The audit pointed out:
score is meaningful only relative to candidate uniqueness, and score
alone is not action-specific evidence.

WI-26 replaces the score-as-gate with a RepairPolicy:
  - required structural features (test_id_required / role_match /
    landmark_match) must match for acceptance.
  - uniqueness_scope (whole_page / within_section / within_row /
    within_dialog) declares where the heal candidate must be unique.
  - allowed_drift_fields names fields known to drift.
  - required_postcondition routes the verifier to an action-specific
    assertion instead of whole-page-signature.
  - medium_confidence_pauses=True forces operator confirmation on
    medium-score heals (default for destructive actions).

Acceptance check (from the plan):
  Two visible "Approve" buttons force ambiguity/human choice even if
  one has high text similarity. Annotator emits RepairPolicy from
  observed fingerprint quality + action risk.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

import pytest

from pilot.skill_models import (
    ElementFingerprint,
    RepairPolicy,
    Skill,
    SkillStep,
)
from pilot.skill_runner import SkillRunner


def _runner() -> SkillRunner:
    skill = Skill(
        name="t",
        steps=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    return SkillRunner(
        session=None,  # type: ignore[arg-type]
        skill=skill,
        params={},
        sessions_dir=Path(tempfile.gettempdir()),
    )


class _StubHealResult:
    """Mimics the locator_repair.HealResult shape just enough to drive
    _policy_reject_reason without spinning up Playwright."""

    def __init__(
        self,
        confidence: str = "high",
        new_fingerprint: ElementFingerprint | None = None,
    ):
        self.confidence = confidence
        self.new_fingerprint = new_fingerprint
        self.reason = "test stub"
        self.backend = "deterministic"


# ---------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------


def test_repair_policy_defaults_match_safe_legacy_behavior() -> None:
    """RepairPolicy defaults to all-false structural requirements and
    medium_confidence_pauses=True. Legacy skills with no policy get
    these defaults via _effective_repair_policy."""
    p = RepairPolicy()
    assert p.test_id_required is False
    assert p.role_match_required is False
    assert p.landmark_match_required is False
    assert p.uniqueness_scope == "whole_page"
    assert p.allowed_drift_fields == []
    assert p.required_postcondition is None
    assert p.medium_confidence_pauses is True


def test_repair_policy_uniqueness_scope_literals() -> None:
    """All declared uniqueness_scope literals are accepted."""
    for scope in (
        "whole_page",
        "within_section",
        "within_row",
        "within_dialog",
    ):
        p = RepairPolicy(uniqueness_scope=scope)  # type: ignore[arg-type]
        assert p.uniqueness_scope == scope


# ---------------------------------------------------------------------
# _effective_repair_policy: default selection by action class
# ---------------------------------------------------------------------


def test_effective_policy_destructive_pauses_on_medium() -> None:
    """Destructive actions (click etc.) get
    medium_confidence_pauses=True by default."""
    runner = _runner()
    step = SkillStep(index=0, action="click")
    pol = runner._effective_repair_policy(step)
    assert pol.medium_confidence_pauses is True


def test_effective_policy_non_destructive_no_medium_pause() -> None:
    """Non-destructive actions (wait / assert / navigate) get
    medium_confidence_pauses=False so observational replay isn't
    gated on operator confirmation."""
    runner = _runner()
    step = SkillStep(index=0, action="wait", wait_ms=100)
    pol = runner._effective_repair_policy(step)
    assert pol.medium_confidence_pauses is False


def test_effective_policy_explicit_overrides_default() -> None:
    """A step's declared repair_policy wins over the action-class
    default."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        repair_policy=RepairPolicy(medium_confidence_pauses=False),
    )
    pol = runner._effective_repair_policy(step)
    assert pol.medium_confidence_pauses is False


# ---------------------------------------------------------------------
# _policy_reject_reason: structural-contract gate
# ---------------------------------------------------------------------


def test_policy_reject_test_id_required_but_missing() -> None:
    """test_id_required=True: a healed candidate without a test_id is
    rejected even on high similarity."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        repair_policy=RepairPolicy(test_id_required=True),
    )
    original = ElementFingerprint(test_id="btn-approve-A")
    new = ElementFingerprint(accessible_name="Approve", tag="button")
    reason = runner._policy_reject_reason(
        step,
        original,
        _StubHealResult(new_fingerprint=new),
    )
    assert reason is not None
    assert "test_id_required" in reason


def test_policy_accept_test_id_required_when_drift_allowed() -> None:
    """allowed_drift_fields can waive an otherwise-required field."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        repair_policy=RepairPolicy(
            test_id_required=True,
            allowed_drift_fields=["test_id"],
        ),
    )
    original = ElementFingerprint(test_id="btn-approve-A")
    new = ElementFingerprint(accessible_name="Approve", tag="button")
    reason = runner._policy_reject_reason(
        step,
        original,
        _StubHealResult(new_fingerprint=new),
    )
    assert reason is None


def test_policy_reject_role_mismatch() -> None:
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        repair_policy=RepairPolicy(role_match_required=True),
    )
    original = ElementFingerprint(role="button", test_id="t")
    new = ElementFingerprint(role="link", test_id="t")
    reason = runner._policy_reject_reason(
        step,
        original,
        _StubHealResult(new_fingerprint=new),
    )
    assert reason is not None
    assert "role_match_required" in reason


def test_policy_reject_landmark_mismatch() -> None:
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        repair_policy=RepairPolicy(landmark_match_required=True),
    )
    original = ElementFingerprint(landmark="confirm-dialog", test_id="t")
    new = ElementFingerprint(landmark="page-shell", test_id="t")
    reason = runner._policy_reject_reason(
        step,
        original,
        _StubHealResult(new_fingerprint=new),
    )
    assert reason is not None
    assert "landmark_match_required" in reason


def test_policy_accept_when_no_required_features() -> None:
    """The default policy (no structural requirements) accepts any
    new_fingerprint -- legacy behavior preserved for skills that don't
    yet declare a RepairPolicy."""
    runner = _runner()
    step = SkillStep(index=0, action="click")  # default policy
    original = ElementFingerprint(test_id="t")
    new = ElementFingerprint(accessible_name="Approve")
    reason = runner._policy_reject_reason(
        step,
        original,
        _StubHealResult(new_fingerprint=new),
    )
    assert reason is None


def test_policy_carries_required_postcondition_for_runner() -> None:
    """The runner reads required_postcondition off the effective
    policy and threads it onto the heal_info dict so the action
    method's verifier can route to an action-specific assertion
    instead of the whole-page-signature legacy check."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        repair_policy=RepairPolicy(
            required_postcondition="url_matches_template"
        ),
    )
    pol = runner._effective_repair_policy(step)
    assert pol.required_postcondition == "url_matches_template"
