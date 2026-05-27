"""WI-37: virtualized / paginated tables modeled as scroll_until.

Plan acceptance check:
  A recording that scrolled past 50 rows to click row 75 can replay
  against a list that has row 75 at any current scroll position.

Tests cover:
  - Schema: ScrollUntilSpec roundtrip.
  - Annotator: a visibility_change event with scroller_selector
    followed by a click whose ancestor chain includes that scroller
    produces a synthetic scroll_until step PREPENDED to the click.
  - Annotator: a click with no preceding scroll does NOT get a
    synthetic step.
  - target_identity_to_selector helper: precedence test_id >
    element_id > row_key > text > None.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _index_scroll_until_steps,
    _assign_synthetic_ids,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    ScrollUntilSpec,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import _target_identity_to_selector


# ----- schema --------------------------------------------------------------


def test_scroll_until_spec_roundtrip() -> None:
    spec = ScrollUntilSpec(
        scroller_fp=ElementFingerprint(test_id="asset-table-scroller"),
        target_identity={"test_id": "row-A-9075"},
        max_scrolls=80,
        page_size_hint=20,
        scroll_direction="down",
    )
    step = SkillStep(
        index=0,
        action="scroll_until",
        scroll_until=spec,
    )
    restored = SkillStep.model_validate(step.model_dump())
    assert restored.scroll_until is not None
    assert (
        restored.scroll_until.scroller_fp.test_id == "asset-table-scroller"
    )
    assert (
        restored.scroll_until.target_identity["test_id"] == "row-A-9075"
    )
    assert restored.scroll_until.max_scrolls == 80
    assert restored.scroll_until.page_size_hint == 20
    assert restored.scroll_until.scroll_direction == "down"


# ----- target_identity_to_selector ----------------------------------------


def test_target_identity_to_selector_precedence() -> None:
    """test_id wins over the others; falls through gracefully."""
    assert (
        _target_identity_to_selector({"test_id": "row-1"})
        == '[data-testid="row-1"]'
    )
    assert (
        _target_identity_to_selector({"element_id": "row-1"})
        == "#row-1"
    )
    assert (
        _target_identity_to_selector({"row_key": "A-9075"})
        == '[data-row-key="A-9075"]'
    )
    assert _target_identity_to_selector({}) is None
    assert _target_identity_to_selector({"foo": "bar"}) is None


# ----- annotator detection -------------------------------------------------


def _make_scroll_event(
    event_id: str,
    sequence: int,
    interaction_id: str,
    scroller_testid: str,
    direction: str = "down",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="visibility_change",
        page_url="http://x",
        event_id=event_id,
        interaction_id=interaction_id,
        sequence=sequence,
        scroller_selector=f'[data-testid="{scroller_testid}"]',
        scroll_direction=direction,  # type: ignore[arg-type]
        source="scroll_observer",
    )


def _click_inside(
    event_id: str,
    sequence: int,
    test_id: str,
    *,
    ancestor_scroller_testid: str,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="button",
            role="button",
            ancestor_chain=[{
                "tag": "div",
                "id": None,
                "testId": ancestor_scroller_testid,
                "role": None,
                "className": None,
            }],
        ),
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def test_scroll_until_detected_on_click_inside_scroller() -> None:
    """Scroll inside [data-testid='asset-scroller'] then click an
    element whose ancestor chain carries that testid -> scroll_until
    keyed by the click's event_id."""
    ev_scroll = _make_scroll_event(
        "scroll1", 1, "i1", "asset-scroller",
    )
    ev_click = _click_inside(
        "click1", 2, "row-A-9075",
        ancestor_scroller_testid="asset-scroller",
    )
    events = _assign_synthetic_ids([ev_scroll, ev_click])
    causality = build_causality_graph(events)
    out = _index_scroll_until_steps(events, causality)
    assert "click1" in out
    spec = out["click1"]
    assert spec.scroller_fp.test_id == "asset-scroller"
    assert spec.target_identity["test_id"] == "row-A-9075"
    assert spec.scroll_direction == "down"


def test_no_scroll_until_when_click_outside_scrolled_container() -> None:
    """Scroll on container A, click happens in container B -> no
    scroll_until emitted (the operator did not scroll to reach this
    click target)."""
    ev_scroll = _make_scroll_event(
        "scroll1", 1, "i1", "different-scroller",
    )
    ev_click = _click_inside(
        "click1", 2, "row-x",
        ancestor_scroller_testid="asset-scroller",
    )
    events = _assign_synthetic_ids([ev_scroll, ev_click])
    causality = build_causality_graph(events)
    out = _index_scroll_until_steps(events, causality)
    assert "click1" not in out


def test_no_scroll_until_without_preceding_scroll() -> None:
    """Click with no scroll events anywhere in the trace -> empty."""
    ev_click = _click_inside(
        "click1", 1, "row-y",
        ancestor_scroller_testid="asset-scroller",
    )
    events = _assign_synthetic_ids([ev_click])
    causality = build_causality_graph(events)
    out = _index_scroll_until_steps(events, causality)
    assert out == {}


def test_build_skill_prepends_scroll_until_step_before_click() -> None:
    """End-to-end: build_skill produces [scroll_until, click] when a
    scroll-then-click pattern is detected."""
    ev_scroll = _make_scroll_event(
        "scroll1", 1, "i1", "asset-scroller",
    )
    ev_click = _click_inside(
        "click1", 2, "row-A-9075",
        ancestor_scroller_testid="asset-scroller",
    )
    skill = build_skill(
        "open_row", [ev_scroll, ev_click],
        auto=True, base_url="http://x",
    )
    assert isinstance(skill, Skill)
    actions = [s.action for s in skill.steps]
    assert actions == ["scroll_until", "click"]
    su_step = skill.steps[0]
    assert su_step.scroll_until is not None
    assert (
        su_step.scroll_until.scroller_fp.test_id == "asset-scroller"
    )
    assert (
        su_step.scroll_until.target_identity["test_id"] == "row-A-9075"
    )
