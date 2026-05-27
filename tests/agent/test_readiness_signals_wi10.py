"""WI-10: observed busy/readiness signals replace spinner-by-convention.

Acceptance check: the Save button's disabled->enabled transition is
captured during teach and the replay waits for it without relying on
the ``status-saving`` naming convention.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pilot.annotate import _index_readiness_signals, build_skill
from pilot.skill_models import (
    DomExpectation,
    ElementFingerprint,
    ExpectedSignals,
    Skill,
    SkillStep,
    TraceEvent,
)


# ---------------------------------------------------------------------------
# Schema: new DomExpectation kinds
# ---------------------------------------------------------------------------


def test_dom_expectation_supports_new_kinds():
    for kind in [
        "aria_busy", "role_progressbar_hidden", "disabled_until_enabled",
        "field_enabled", "text_transition", "selector_hidden",
    ]:
        de = DomExpectation(kind=kind, selector="[data-testid='x']")  # type: ignore[arg-type]
        assert de.kind == kind


def test_text_transition_carries_target_text():
    de = DomExpectation(
        kind="text_transition",
        selector="[data-testid='btn-save']",
        text="Save",
    )
    assert de.text == "Save"


def test_dom_expectation_roundtrip_with_text():
    de = DomExpectation(
        kind="text_transition",
        selector="button.save",
        text="Save",
        timeout_ms=4000,
    )
    blob = de.model_dump_json()
    restored = DomExpectation.model_validate_json(blob)
    assert restored.kind == "text_transition"
    assert restored.text == "Save"


# ---------------------------------------------------------------------------
# Annotator: convert observed readiness into expected_signals.dom
# ---------------------------------------------------------------------------


def _click_event(eid: str, label: str = "btn-save") -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(test_id=label),
        page_url="http://x/asset/1",
        event_id=eid,
        interaction_id=eid,
        caused_by=None,
        sequence=1,
        source="user_click",
    )


def _readiness_event(eid: str, caused_by: str, kind: str, selector: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="dom_mutation",
        page_url="http://x/asset/1",
        event_id=eid,
        caused_by=caused_by,
        interaction_id=caused_by,
        sequence=2,
        source="readiness_observer",
        raw_event_kind="readiness_transition",
        mutation_summary={
            "readiness": {
                "kind": kind,
                "selector": selector,
            }
        },
    )


def test_index_readiness_signals_groups_by_cause():
    click = _click_event("save-1", "btn-save")
    r1 = _readiness_event(
        "r1", "save-1", "disabled_until_enabled", "[data-testid='btn-save']"
    )
    r2 = _readiness_event(
        "r2", "save-1", "aria_busy", "[data-testid='btn-save']"
    )
    out = _index_readiness_signals([click, r1, r2])
    assert "save-1" in out
    assert len(out["save-1"]) == 2
    kinds = {de.kind for de in out["save-1"]}
    assert kinds == {"disabled_until_enabled", "aria_busy"}


def test_index_readiness_ignores_unknown_kinds():
    click = _click_event("c1")
    bogus = _readiness_event("r1", "c1", "made_up_kind", "[data-testid='x']")
    out = _index_readiness_signals([click, bogus])
    assert out == {}


def test_index_readiness_skips_uncaused_dom_mutations():
    """Background DOM activity has caused_by=None; the annotator must
    not turn that into a readiness expectation."""
    click = _click_event("c1")
    bg = TraceEvent(
        ts=datetime.utcnow(),
        kind="dom_mutation",
        page_url="http://x",
        event_id="bg-1",
        caused_by=None,
        mutation_summary={"readiness": {"kind": "aria_busy", "selector": "#x"}},
    )
    out = _index_readiness_signals([click, bg])
    assert out == {}


def test_build_skill_attaches_readiness_to_step():
    """The acceptance check: a Save click that observes its own
    disabled-until-enabled transition produces a step with
    expected_signals.dom = [disabled_until_enabled selector]."""
    click = _click_event("save-1", "btn-save")
    readiness = _readiness_event(
        "r1", "save-1",
        "disabled_until_enabled",
        "[data-testid='btn-save']",
    )
    skill = build_skill(
        skill_name="t", events=[click, readiness], auto=True
    )
    # One step (the click), readiness event is informational.
    assert len(skill.steps) == 1
    s = skill.steps[0]
    assert s.expected_signals is not None
    dom = s.expected_signals.dom
    assert len(dom) == 1
    assert dom[0].kind == "disabled_until_enabled"
    assert dom[0].selector == "[data-testid='btn-save']"


def test_build_skill_aggregates_multiple_readiness_per_step():
    """A Save button can observe BOTH disabled->enabled AND aria-busy
    clear in the same effect window. Both become DomExpectations."""
    click = _click_event("save-1", "btn-save")
    r1 = _readiness_event(
        "r1", "save-1",
        "disabled_until_enabled", "[data-testid='btn-save']",
    )
    r2 = _readiness_event(
        "r2", "save-1",
        "aria_busy", "[data-testid='btn-save']",
    )
    skill = build_skill(
        skill_name="t", events=[click, r1, r2], auto=True
    )
    s = skill.steps[0]
    assert s.expected_signals is not None
    kinds = {de.kind for de in s.expected_signals.dom}
    assert kinds == {"disabled_until_enabled", "aria_busy"}


def test_steps_without_observed_readiness_get_no_signals():
    """Legacy traces (no readiness watcher) produce steps without
    expected_signals -- the runner falls back to the legacy spinner
    convention path."""
    click = _click_event("c1", "btn-x")
    skill = build_skill(skill_name="t", events=[click], auto=True)
    s = skill.steps[0]
    assert s.expected_signals is None


# ---------------------------------------------------------------------------
# Skill-level roundtrip
# ---------------------------------------------------------------------------


def test_skill_with_readiness_signals_roundtrip():
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn-save"),
        expected_signals=ExpectedSignals(
            dom=[
                DomExpectation(
                    kind="disabled_until_enabled",
                    selector="[data-testid='btn-save']",
                ),
                DomExpectation(
                    kind="aria_busy",
                    selector="[data-testid='btn-save']",
                ),
            ]
        ),
    )
    skill = Skill(name="t", steps=[step])
    blob = skill.model_dump_json()
    restored = Skill.model_validate_json(blob)
    dom = restored.steps[0].expected_signals.dom  # type: ignore[union-attr]
    assert {de.kind for de in dom} == {"disabled_until_enabled", "aria_busy"}
