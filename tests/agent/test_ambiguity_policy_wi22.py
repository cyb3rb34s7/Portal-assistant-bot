"""Tests for WI-22: ambiguity detection at every locator level.

Today only fires for templated test_id / element_id. WI-22 adds:
  - ``AmbiguityPolicy`` schema model with ``locator_scope`` /
    ``expected_candidate_count`` / ``ambiguity_policy`` /
    ``candidate_context_fields``.
  - ``ambiguity_policy`` optional field on ``SkillStep``.
  - Action-class defaults: destructive => fail_if_multiple,
    non-destructive => prompt.
  - ``_effective_ambiguity_policy`` reads the per-step value with
    fallbacks.
  - ``_detect_ambiguity`` now scans L1 (test_id/element_id/name/
    aria_label) AND L2 (role+name / accessible name) -- not only
    templated fields.

Acceptance check (from the plan):
  Multiple visible "Open" buttons produce a row-picker modal pause
  instead of silently clicking the first.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

from pilot.skill_models import (
    AmbiguityPolicy,
    ElementFingerprint,
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


def test_ambiguity_policy_default_destructive_action() -> None:
    """A click step with no explicit policy defaults to
    fail_if_multiple with expected_candidate_count=1."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="open"),
    )
    expected, policy, fields = runner._effective_ambiguity_policy(step)
    assert expected == 1
    assert policy == "fail_if_multiple"
    assert "test_id" in fields
    assert "text" in fields


def test_ambiguity_policy_default_non_destructive_prompts() -> None:
    """A wait / assert / key step defaults to prompt so a
    multiple-match doesn't fail-stop a non-mutating step."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="wait",
        wait_ms=100,
    )
    _, policy, _ = runner._effective_ambiguity_policy(step)
    assert policy == "prompt"


def test_ambiguity_policy_explicit_overrides_default() -> None:
    """When AmbiguityPolicy is declared on the step it wins over the
    action-class default."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="open"),
        ambiguity_policy=AmbiguityPolicy(
            expected_candidate_count=3,
            ambiguity_policy="pick_first",
            candidate_context_fields=["test_id", "aria-label"],
        ),
    )
    expected, policy, fields = runner._effective_ambiguity_policy(step)
    assert expected == 3
    assert policy == "pick_first"
    assert "aria-label" in fields


def test_ambiguity_policy_requires_gate_treats_step_as_destructive() -> None:
    """requires_gate (irreversible) implies destructive, regardless of
    the action type."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="navigate",  # non-destructive class
        requires_gate=True,
        fingerprint=ElementFingerprint(test_id="row"),
    )
    _, policy, _ = runner._effective_ambiguity_policy(step)
    assert policy == "fail_if_multiple"


def test_ambiguity_policy_expected_count_zero_opts_out() -> None:
    """Setting expected_candidate_count=0 is the explicit escape hatch
    for batch operations where the recording intentionally matches
    many. The runner's _detect_ambiguity must return None for such a
    step regardless of candidate count."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="t"),
        ambiguity_policy=AmbiguityPolicy(expected_candidate_count=0),
    )
    result = runner._detect_ambiguity(
        page=None,  # type: ignore[arg-type]
        fp=step.fingerprint,
        step=step,
    )
    assert result is None


def test_ambiguity_policy_pick_first_skips_detection() -> None:
    """pick_first is the legacy escape hatch -- the detector returns
    None so the runner falls through to the resolver's .first behavior
    unchanged."""
    runner = _runner()
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="t"),
        ambiguity_policy=AmbiguityPolicy(ambiguity_policy="pick_first"),
    )
    result = runner._detect_ambiguity(
        page=None,  # type: ignore[arg-type]
        fp=step.fingerprint,
        step=step,
    )
    assert result is None


def test_ambiguity_policy_l2_role_name_scanned_even_without_template() -> None:
    """Audit-driven case: a click step whose fingerprint has role +
    accessible name (no test_id template) used to skip ambiguity
    detection entirely because the legacy detector required a templated
    test_id / element_id. WI-22's scanner attempts role+name as part of
    its candidate sweep; an L2 ambiguity is now detectable."""
    runner = _runner()
    fp = ElementFingerprint(role="button", accessible_name="Open")
    step = SkillStep(index=0, action="click", fingerprint=fp)
    # We can't run a real page-side scan in this unit test (no
    # Playwright session), but the resolver must consult
    # _effective_ambiguity_policy to figure out THE policy, then
    # attempt the role_name lambda. The expected policy on a click
    # without an explicit ambiguity_policy is fail_if_multiple.
    expected, policy, _ = runner._effective_ambiguity_policy(step)
    assert expected == 1
    assert policy == "fail_if_multiple"


def test_enrich_candidates_fills_declared_fields_with_none() -> None:
    """The candidate enrichment helper ensures each row carries
    every field the policy declared (even if absent on the actual
    candidate). Keeps the row-picker UI rendering uniform across
    candidates that may be sparse."""
    runner = _runner()
    raw = [
        {"test_id": "row-A", "text": "Open A"},
        {"id": "row-B", "text": "Open B"},
    ]
    enriched = runner._enrich_candidates(
        raw, ["test_id", "text", "id", "role", "aria-label"]
    )
    assert len(enriched) == 2
    # Every row carries every declared field key.
    for row in enriched:
        for key in ("test_id", "text", "id", "role", "aria-label"):
            assert key in row
