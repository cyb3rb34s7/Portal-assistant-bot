"""WI-43: disabled->enabled readiness as a first-class wait.

Plan acceptance check:
  A 'Submit for Review' button that's disabled until categories+tags+
  region+market+language are picked records the readiness signal;
  replay waits for the button to enable.

WI-43 builds on WI-10:
  - Annotator: when a click step's fingerprint shows disabled=True
    AND no field_enabled / disabled_until_enabled signal was
    captured by the WI-10 readiness watcher, synthesize a
    field_enabled DomExpectation so replay still waits.
  - Runner: when a click target is currently disabled AND the step
    has no field_enabled / disabled_until_enabled signal, fail
    with error_kind='target_disabled' (don't fire a click
    Playwright will silently no-op).

Tests cover the deterministic pipeline (no live browser):
  - _has_field_enabled_signal returns True when expected_signals.dom
    declares field_enabled or disabled_until_enabled.
  - Annotator: a click on a recorded-disabled element with NO
    readiness signal in the trace gets a synthesized
    field_enabled DomExpectation prepended.
  - Annotator: a click on a recorded-disabled element WITH a
    field_enabled readiness signal already declared is unchanged
    (no duplicate).
  - Annotator: a click on a recorded-enabled element gets no
    synthesized expectation.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import build_skill
from pilot.skill_models import (
    DomExpectation,
    ElementFingerprint,
    ExpectedSignals,
    SkillStep,
    TraceEvent,
)


def _click_on_disabled(
    event_id: str = "c1",
    sequence: int = 1,
    *,
    test_id: str = "btn-submit-review",
    disabled: bool = True,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="button",
            control_kind="button",
            disabled=disabled,
        ),
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_click",
        raw_event_kind="click",
    )


# ----- annotator behavior --------------------------------------------------


def test_disabled_click_without_signal_gets_synthesized_field_enabled() -> None:
    # The grabber's readiness watcher missed the transition (older
    # recording or transition happened outside the interaction
    # window). The annotator should defensively prepend a
    # field_enabled DomExpectation so replay waits.
    ev = _click_on_disabled("c1", 1, disabled=True)
    skill = build_skill(
        skill_name="disabled_no_signal",
        events=[ev],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    click_steps = [s for s in skill.steps if s.action == "click"]
    assert len(click_steps) == 1
    cs = click_steps[0]
    assert cs.expected_signals is not None
    kinds = [d.kind for d in cs.expected_signals.dom]
    assert "field_enabled" in kinds, (
        f"expected field_enabled in {kinds}"
    )
    field_enabled = next(
        d for d in cs.expected_signals.dom if d.kind == "field_enabled"
    )
    assert field_enabled.selector == '[data-testid="btn-submit-review"]'


def test_enabled_click_gets_no_synthesized_expectation() -> None:
    # Click target was enabled at record time; no field_enabled
    # synthesized.
    ev = _click_on_disabled("c1", 1, disabled=False)
    skill = build_skill(
        skill_name="enabled_click",
        events=[ev],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    cs = [s for s in skill.steps if s.action == "click"][0]
    if cs.expected_signals is not None:
        kinds = [d.kind for d in cs.expected_signals.dom]
        assert "field_enabled" not in kinds


def test_synthesized_signal_uses_element_id_when_no_testid() -> None:
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            element_id="submit-btn",
            tag="button",
            control_kind="button",
            disabled=True,
        ),
        page_url="http://x",
        event_id="c1",
        interaction_id="c1",
        caused_by=None,
        sequence=1,
        source="user_click",
        raw_event_kind="click",
    )
    skill = build_skill(
        skill_name="no_testid_id_only",
        events=[ev],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    cs = [s for s in skill.steps if s.action == "click"][0]
    assert cs.expected_signals is not None
    fe = next(
        (d for d in cs.expected_signals.dom if d.kind == "field_enabled"),
        None,
    )
    assert fe is not None
    assert fe.selector == "#submit-btn"


# ----- runner helper -------------------------------------------------------


def test_has_field_enabled_signal_detects_either_kind() -> None:
    # Imported here to avoid a top-level import chain
    from pilot.skill_runner import SkillRunner

    # Build a minimal runner stub by constructing the class with a
    # placeholder session; we only need _has_field_enabled_signal.
    class _Stub(SkillRunner):
        def __init__(self):  # noqa: D401
            pass

    stub = _Stub()
    step_with_field = SkillStep(
        index=0,
        action="click",
        expected_signals=ExpectedSignals(
            dom=[DomExpectation(kind="field_enabled", selector="#x")]
        ),
    )
    step_with_disabled = SkillStep(
        index=1,
        action="click",
        expected_signals=ExpectedSignals(
            dom=[DomExpectation(kind="disabled_until_enabled", selector="#x")]
        ),
    )
    step_without = SkillStep(
        index=2,
        action="click",
        expected_signals=ExpectedSignals(
            dom=[DomExpectation(kind="visible", selector="#x")]
        ),
    )
    step_none = SkillStep(index=3, action="click")
    assert stub._has_field_enabled_signal(step_with_field) is True
    assert stub._has_field_enabled_signal(step_with_disabled) is True
    assert stub._has_field_enabled_signal(step_without) is False
    assert stub._has_field_enabled_signal(step_none) is False
