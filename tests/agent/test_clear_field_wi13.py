"""WI-13: preserve clear-field actions as real value transitions.

Acceptance check (from the plan):
  Trace title="Old" -> clear -> save produces a change/fill with value
  "" and replays to empty field.

The annotator now:
  - keeps empty input_change events (the legacy heuristic dropped them)
  - attaches a ValueTransition to each change step with from_recorded
    + to_recorded + clear_intent
  - marks clear_intent=True when before was non-empty, after is empty,
    AND a commit signal follows (submit / key / navigate / click /
    blur-to-another-field).

The runner:
  - reads value_transition.clear_intent and forces value="" regardless
    of param resolution
  - verifies the field is empty post-fill, surfacing
    error_kind="clear_intent_unmet" when the clear didn't stick.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pilot.annotate import (
    build_skill,
    derive_value_transition,
    filter_events,
)
from pilot.skill_models import (
    ElementFingerprint,
    SkillStep,
    Skill,
    TraceEvent,
    ValueTransition,
)


def _input_change(
    event_id: str,
    value: str,
    *,
    value_before: str | None = None,
    target_testid: str = "input-title",
    sequence: int = 1,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id=target_testid, tag="input", input_type="text"
        ),
        value=value,
        value_before=value_before,
        page_url="http://x/edit",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_input",
    )


def _click(
    event_id: str, target_testid: str = "btn-save", sequence: int = 2
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(test_id=target_testid, tag="button"),
        page_url="http://x/edit",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_click",
    )


# ---------------------------------------------------------------------------
# filter_events no longer drops empty input_change events (WI-13)
# ---------------------------------------------------------------------------


def test_filter_events_preserves_empty_input_change() -> None:
    """Legacy heuristic dropped same-target empty-after-empty events.
    WI-13 stops that drop because the empty value is meaningful when
    paired with a non-empty value_before -- it's a deliberate clear.
    """
    e1 = _input_change("e1", "Old Title", value_before=None, sequence=1)
    e2 = _input_change("e2", "", value_before="Old Title", sequence=2)
    events = filter_events([e1, e2])
    # Both events survive -- the empty one is no longer silently dropped.
    assert len(events) == 2
    assert events[1].value == ""
    assert events[1].value_before == "Old Title"


# ---------------------------------------------------------------------------
# Unit: derive_value_transition computes clear_intent
# ---------------------------------------------------------------------------


def test_derive_value_transition_marks_clear_intent_when_commit_follows() -> None:
    """before='Old' -> after='' + a later commit signal (click on
    save) -> clear_intent=True."""
    e1 = _input_change("e1", "", value_before="Old Title", sequence=1)
    save = _click("save1", "btn-save", sequence=2)
    transition = derive_value_transition(e1, [e1, save], 0)
    assert transition is not None
    assert transition.from_recorded == "Old Title"
    assert transition.to_recorded == ""
    assert transition.clear_intent is True


def test_derive_value_transition_no_commit_means_no_clear_intent() -> None:
    """before='Old' -> after='' but NO commit signal follows -> the
    operator may have abandoned the edit; clear_intent=False keeps
    behavior conservative."""
    e1 = _input_change("e1", "", value_before="Old Title", sequence=1)
    # No follow-up events.
    transition = derive_value_transition(e1, [e1], 0)
    assert transition is not None
    assert transition.clear_intent is False


def test_derive_value_transition_non_clear_change_keeps_intent_false() -> None:
    """A normal type-into-empty-field is NOT a clear: before=''
    or None, after=non-empty -> clear_intent=False, transition records
    the new value for audit."""
    e1 = _input_change("e1", "New Value", value_before="", sequence=1)
    transition = derive_value_transition(e1, [e1], 0)
    assert transition is not None
    assert transition.to_recorded == "New Value"
    assert transition.clear_intent is False


def test_derive_value_transition_legacy_trace_skips_when_no_value_or_before() -> None:
    """Pre-WI-13 trace: value_before is None AND value is non-empty.
    No actionable transition; return None so the step omits the
    field entirely (back-compat with v1 skills)."""
    e1 = _input_change("e1", "Hello", value_before=None, sequence=1)
    transition = derive_value_transition(e1, [e1], 0)
    assert transition is None


def test_derive_value_transition_legacy_clear_emits_transition_without_intent() -> None:
    """A pre-WI-13 trace where the operator cleared a field but the
    grabber didn't capture value_before: emit a transition with
    from_recorded=None, to_recorded='', clear_intent=False so the
    runner still calls fill('')."""
    e1 = _input_change("e1", "", value_before=None, sequence=1)
    save = _click("s1", "btn-save", sequence=2)
    transition = derive_value_transition(e1, [e1, save], 0)
    assert transition is not None
    assert transition.from_recorded is None
    assert transition.to_recorded == ""
    assert transition.clear_intent is False


def test_derive_value_transition_only_non_input_returns_none() -> None:
    """Non-input events have no transition (clicks, key, etc)."""
    click = _click("c1", "btn-x", sequence=1)
    assert derive_value_transition(click, [click], 0) is None


# ---------------------------------------------------------------------------
# End-to-end: build_skill emits a change step with clear_intent
# ---------------------------------------------------------------------------


def test_build_skill_emits_clear_intent_step() -> None:
    """The canonical workflow: title was 'Old', operator cleared and
    saved. build_skill produces a change step with value_transition.
    clear_intent=True."""
    clear = _input_change("e1", "", value_before="Old Title", sequence=1)
    save = _click("save1", "btn-save", sequence=2)
    skill = build_skill(skill_name="t", events=[clear, save], auto=True)
    # Two steps: the clear (change) and the save click.
    assert len(skill.steps) == 2
    change_step = skill.steps[0]
    assert change_step.action == "change"
    assert change_step.value == ""
    vt = change_step.value_transition
    assert vt is not None
    assert vt.from_recorded == "Old Title"
    assert vt.to_recorded == ""
    assert vt.clear_intent is True


def test_build_skill_normal_fill_no_clear_intent() -> None:
    fill = _input_change("e1", "New Title", value_before="", sequence=1)
    save = _click("save1", "btn-save", sequence=2)
    skill = build_skill(skill_name="t", events=[fill, save], auto=True)
    vt = skill.steps[0].value_transition
    assert vt is not None
    assert vt.clear_intent is False
    assert vt.to_recorded == "New Title"


# ---------------------------------------------------------------------------
# Roundtrip: ValueTransition + value_before persist in JSON
# ---------------------------------------------------------------------------


def test_value_transition_roundtrip() -> None:
    step = SkillStep(
        index=0,
        action="change",
        fingerprint=ElementFingerprint(test_id="input-title"),
        value="",
        value_transition=ValueTransition(
            from_recorded="Old Title",
            to_recorded="",
            clear_intent=True,
        ),
    )
    skill = Skill(name="t", steps=[step])
    blob = skill.model_dump_json()
    restored = Skill.model_validate_json(blob)
    vt = restored.steps[0].value_transition
    assert vt is not None
    assert vt.from_recorded == "Old Title"
    assert vt.to_recorded == ""
    assert vt.clear_intent is True


def test_trace_event_value_before_roundtrip() -> None:
    ev = _input_change("e1", "", value_before="Old", sequence=1)
    blob = ev.model_dump_json()
    restored = TraceEvent.model_validate_json(blob)
    assert restored.value_before == "Old"
    assert restored.value == ""
