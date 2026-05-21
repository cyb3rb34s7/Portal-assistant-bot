"""Unit tests for the WI-02 causality + identity layer.

Pins behavior:
  - synthetic event_ids are assigned to legacy traces in deterministic order
  - the causality graph indexes events by id, interaction, parent-child,
    and identifies user-action (root) events
  - SkillSteps built from traces carry their source raw_event_id in
    provenance
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pilot.annotate import (
    _assign_synthetic_ids,
    build_causality_graph,
    build_skill,
    filter_events,
)
from pilot.skill_models import TraceEvent


def _ev(kind: str, **kw):
    base = {"ts": datetime.utcnow(), "kind": kind, "page_url": "http://x"}
    base.update(kw)
    return TraceEvent.model_validate(base)


def test_synthetic_id_assignment_is_deterministic() -> None:
    """Legacy traces (no event_id from grabber) get synthetic IDs based on
    file order. Re-annotating the same trace yields the same IDs."""
    events = [
        _ev("click", page_url="http://a"),
        _ev("input_change", value="hello", page_url="http://a"),
        _ev("submit", page_url="http://a"),
    ]
    _assign_synthetic_ids(events)
    ids = [e.event_id for e in events]
    assert ids == ["synth-000001", "synth-000002", "synth-000003"]
    # Re-running on the same already-stamped list is a no-op
    _assign_synthetic_ids(events)
    assert [e.event_id for e in events] == ids


def test_synthetic_id_preserves_grabber_ids() -> None:
    """Events that ALREADY have an event_id from the grabber keep theirs;
    only events missing one get synthetic stamps."""
    events = [
        _ev("click", event_id="real-click-xyz"),
        _ev("input_change", value="hi"),  # no id -> gets synthetic
    ]
    _assign_synthetic_ids(events)
    assert events[0].event_id == "real-click-xyz"
    assert events[1].event_id.startswith("synth-")


def test_causality_graph_indexes_by_all_keys() -> None:
    """The causality graph exposes by_id, by_interaction, children_of,
    and user_actions — the four lookups subsequent WIs need."""
    click = _ev(
        "click",
        event_id="c1",
        interaction_id="int-1",
        caused_by=None,
        source="user_click",
    )
    nav = _ev(
        "navigate",
        event_id="n1",
        interaction_id="int-1",
        caused_by="c1",
        source="history.pushState",
        url="/asset/A-9003",
    )
    manual_nav = _ev(
        "navigate",
        event_id="n2",
        interaction_id=None,
        caused_by=None,
        source="initial_load",
    )

    g = build_causality_graph([click, nav, manual_nav])
    assert set(g["by_id"].keys()) == {"c1", "n1", "n2"}
    assert g["by_interaction"]["int-1"] == ["c1", "n1"]
    assert g["children_of"]["c1"] == ["n1"]
    # User actions are events without a cause -- both the click AND the
    # manually-initiated navigation count.
    assert set(g["user_actions"]) == {"c1", "n2"}


def test_filter_events_accepts_causality_graph() -> None:
    """F-05: filter_events accepts the causality graph as a parameter so
    downstream WIs (WI-06/13/14) can replace legacy heuristics with
    causality-aware logic. Today's body doesn't consume it -- this
    test pins the wiring contract so future refactors don't drop the
    parameter accidentally."""
    click = _ev("click", event_id="c1", interaction_id="int-1")
    nav = _ev(
        "navigate",
        event_id="n1",
        interaction_id="int-1",
        caused_by="c1",
        url="/asset/A-9003",
    )
    g = build_causality_graph([click, nav])
    # Passing the graph must not change behavior versus not passing it.
    out_with = filter_events([click, nav], causality=g)
    out_without = filter_events([click, nav])
    assert [e.event_id for e in out_with] == [e.event_id for e in out_without]


def test_step_provenance_carries_raw_event_id() -> None:
    """A SkillStep built from a single TraceEvent has provenance with
    that event's id."""
    events = [
        _ev("click", event_id="evt-click-1"),
    ]
    skill = build_skill(
        skill_name="t",
        events=events,
        auto=True,
    )
    assert len(skill.steps) == 1
    prov = skill.steps[0].provenance
    assert prov is not None
    assert prov.raw_event_ids == ["evt-click-1"]
    assert prov.cluster_kind == "single_event"
    assert prov.detection_method == "deterministic"
