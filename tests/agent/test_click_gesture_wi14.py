"""WI-14: replace duplicate-click time dedupe with click gesture +
effect classification.

Acceptance check (from the plan):
  - Double-click trace produces a double-click action.
  - Two accordion toggles are preserved or converted into final
    desired state.

The annotator no longer drops clicks under a 200ms window. Instead it
relies on the grabber-captured click_detail (MouseEvent.detail count)
and target_state_before/after to classify each click as single /
double / repeat / toggle / open / close.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pilot.annotate import (
    _state_diff,
    build_skill,
    classify_click_gesture,
    filter_events,
)
from pilot.skill_models import (
    ElementFingerprint,
    Skill,
    SkillStep,
    TraceEvent,
)


def _click(
    event_id: str,
    *,
    target_testid: str = "btn",
    detail: int = 1,
    ts: datetime | None = None,
    state_before: dict | None = None,
    state_after: dict | None = None,
    sequence: int = 1,
) -> TraceEvent:
    return TraceEvent(
        ts=ts or datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(test_id=target_testid, tag="button"),
        page_url="http://x/",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_click",
        click_detail=detail,
        target_state_before=state_before,
        target_state_after=state_after,
    )


# ---------------------------------------------------------------------------
# filter_events: only drops transport-duplicate (same event_id)
# ---------------------------------------------------------------------------


def test_filter_events_keeps_close_repeated_clicks() -> None:
    """The audit-flagged 200ms heuristic dropped intentional double-
    clicks. WI-14: filter_events keeps both as long as their event_ids
    differ."""
    base = datetime.utcnow()
    c1 = _click("c1", target_testid="btn-cell", detail=1, ts=base, sequence=1)
    c2 = _click(
        "c2", target_testid="btn-cell", detail=2,
        ts=base + timedelta(milliseconds=100), sequence=2,
    )
    out = filter_events([c1, c2])
    assert len(out) == 2


def test_filter_events_drops_transport_duplicate_only() -> None:
    """Same event_id twice = transport glitch. Drop the duplicate."""
    base = datetime.utcnow()
    c1 = _click("same-id", target_testid="btn-x", ts=base, sequence=1)
    c2 = _click("same-id", target_testid="btn-x", ts=base, sequence=2)
    out = filter_events([c1, c2])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# classify_click_gesture: per-event semantic classification
# ---------------------------------------------------------------------------


def test_classify_double_click_from_detail() -> None:
    """detail >= 2 -> double, regardless of state."""
    c = _click("c1", detail=2)
    assert classify_click_gesture(c, prior_clicks=[]) == "double"


def test_classify_open_from_aria_expanded_false_to_true() -> None:
    c = _click(
        "c1",
        state_before={"aria_expanded": "false", "disabled": None},
        state_after={"aria_expanded": "true", "disabled": None},
    )
    assert classify_click_gesture(c, prior_clicks=[]) == "open"


def test_classify_close_from_aria_expanded_true_to_false() -> None:
    c = _click(
        "c1",
        state_before={"aria_expanded": "true", "disabled": None},
        state_after={"aria_expanded": "false", "disabled": None},
    )
    assert classify_click_gesture(c, prior_clicks=[]) == "close"


def test_classify_toggle_when_aria_checked_flips() -> None:
    c = _click(
        "c1",
        state_before={"aria_checked": "false", "disabled": None},
        state_after={"aria_checked": "true", "disabled": None},
    )
    assert classify_click_gesture(c, prior_clicks=[]) == "toggle"


def test_classify_repeat_when_same_target_no_state_change() -> None:
    """Two clicks on the same target, neither a double-click, no state
    change observed -> repeat. The runner can decide to fire N times
    or short-circuit; the classifier just labels the pattern."""
    base = datetime.utcnow()
    prev = _click("c1", target_testid="btn-inc", ts=base, sequence=1)
    cur = _click(
        "c2", target_testid="btn-inc", detail=1,
        ts=base + timedelta(milliseconds=400), sequence=2,
    )
    assert classify_click_gesture(cur, prior_clicks=[prev]) == "repeat"


def test_classify_single_when_no_state_change_and_no_prior() -> None:
    c = _click(
        "c1", detail=1,
        state_before={"aria_expanded": None},
        state_after={"aria_expanded": None},
    )
    assert classify_click_gesture(c, prior_clicks=[]) == "single"


def test_classify_returns_none_for_non_click() -> None:
    nav = TraceEvent(
        ts=datetime.utcnow(),
        kind="navigate",
        url="http://x/",
        page_url="http://x/",
        event_id="n1",
        sequence=1,
    )
    assert classify_click_gesture(nav, prior_clicks=[]) is None


# ---------------------------------------------------------------------------
# _state_diff: diff helper
# ---------------------------------------------------------------------------


def test_state_diff_returns_per_attr_changes() -> None:
    diff = _state_diff(
        {"aria_expanded": "false", "disabled": "true"},
        {"aria_expanded": "true", "disabled": "true"},
    )
    assert diff == {"aria_expanded": ["false", "true"]}


def test_state_diff_empty_when_no_change() -> None:
    diff = _state_diff(
        {"aria_expanded": "false"}, {"aria_expanded": "false"}
    )
    assert diff == {}


def test_state_diff_handles_missing_snapshots() -> None:
    assert _state_diff(None, None) == {}
    diff = _state_diff(None, {"aria_expanded": "true"})
    assert diff == {"aria_expanded": [None, "true"]}


# ---------------------------------------------------------------------------
# End-to-end: build_skill stamps gesture + effect_signature
# ---------------------------------------------------------------------------


def test_build_skill_records_double_click_gesture() -> None:
    """A grid cell that the operator double-clicked to edit. The
    annotator stamps click_gesture='double' so the runner picks
    Playwright's dblclick()."""
    c = _click("c1", target_testid="cell-edit", detail=2)
    skill = build_skill(skill_name="t", events=[c], auto=True)
    assert len(skill.steps) == 1
    s = skill.steps[0]
    assert s.action == "click"
    assert s.click_gesture == "double"


def test_build_skill_records_toggle_with_effect_signature() -> None:
    """Accordion header click that flipped aria-expanded: the step
    carries click_gesture='open' and effect_signature shows the diff."""
    c = _click(
        "c1", target_testid="accordion-header",
        state_before={"aria_expanded": "false"},
        state_after={"aria_expanded": "true"},
    )
    skill = build_skill(skill_name="t", events=[c], auto=True)
    s = skill.steps[0]
    assert s.click_gesture == "open"
    assert s.effect_signature == {"aria_expanded": ["false", "true"]}


def test_build_skill_preserves_two_accordion_toggles() -> None:
    """Two clicks on the same accordion header that both toggle the
    state are NOT collapsed by filter_events (the audit-flagged 200ms
    drop is gone). The annotator emits two steps with distinct
    gestures: open then close."""
    base = datetime.utcnow()
    c1 = _click(
        "c1", target_testid="accordion-header",
        state_before={"aria_expanded": "false"},
        state_after={"aria_expanded": "true"},
        ts=base, sequence=1,
    )
    c2 = _click(
        "c2", target_testid="accordion-header",
        state_before={"aria_expanded": "true"},
        state_after={"aria_expanded": "false"},
        ts=base + timedelta(milliseconds=150), sequence=2,
    )
    skill = build_skill(skill_name="t", events=[c1, c2], auto=True)
    assert len(skill.steps) == 2
    assert skill.steps[0].click_gesture == "open"
    assert skill.steps[1].click_gesture == "close"


def test_build_skill_double_click_under_200ms_survives() -> None:
    """The legacy heuristic dropped the second click. WI-14 keeps both
    because their event_ids differ; the annotator stamps the second
    one as 'double' if the browser reported detail=2."""
    base = datetime.utcnow()
    c1 = _click("c1", target_testid="cell-edit", detail=1, ts=base, sequence=1)
    c2 = _click(
        "c2", target_testid="cell-edit", detail=2,
        ts=base + timedelta(milliseconds=100), sequence=2,
    )
    skill = build_skill(skill_name="t", events=[c1, c2], auto=True)
    assert len(skill.steps) == 2
    # Second click classified as double; runner will use dblclick().
    assert skill.steps[1].click_gesture == "double"


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


def test_click_gesture_and_effect_signature_roundtrip() -> None:
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn"),
        click_gesture="double",
        effect_signature={"aria_pressed": ["false", "true"]},
    )
    skill = Skill(name="t", steps=[step])
    blob = skill.model_dump_json()
    restored = Skill.model_validate_json(blob)
    rs = restored.steps[0]
    assert rs.click_gesture == "double"
    assert rs.effect_signature == {"aria_pressed": ["false", "true"]}


def test_trace_event_click_metadata_roundtrip() -> None:
    ev = _click(
        "c1",
        detail=2,
        state_before={"aria_expanded": "false"},
        state_after={"aria_expanded": "true"},
    )
    blob = ev.model_dump_json()
    restored = TraceEvent.model_validate_json(blob)
    assert restored.click_detail == 2
    assert restored.target_state_before == {"aria_expanded": "false"}
    assert restored.target_state_after == {"aria_expanded": "true"}
