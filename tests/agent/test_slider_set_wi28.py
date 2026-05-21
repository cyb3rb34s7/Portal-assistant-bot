"""WI-28: capture + replay range sliders.

Plan acceptance check:
  Moving the slider during teach emits ONE ``slider_set`` step; replay
  sets a DIFFERENT value and verifies it.

Tests cover the deterministic pipeline (no live browser):
  - Schema roundtrip: SliderSpec persists.
  - Detector: a drag burst (multiple input_change on range_slider)
    collapses into ONE slider_set cluster anchored on the LAST event.
  - Annotator: build_skill emits ONE slider_set step + a number_range
    SkillParam with ParamConstraints(min, max) populated from the
    HTML attrs.
  - Runner stub: slider_set is no longer in _UNIMPLEMENTED_ACTIONS.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_slider_set_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    Skill,
    SkillStep,
    SliderSpec,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS


def _slider_change(
    event_id: str,
    value: str,
    sequence: int,
    test_id: str = "input-priority",
    raw_event_kind: str = "input",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="input",
            input_type="range",
            control_kind="range_slider",
            value_kind="number",
            min="0",
            max="100",
            step="5",
        ),
        value=value,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_change",
        raw_event_kind=raw_event_kind,
    )


# ----- schema --------------------------------------------------------------


def test_slider_spec_roundtrip() -> None:
    spec = SliderSpec(
        value_param="priority",
        min=0.0,
        max=100.0,
        step=5.0,
        orientation="horizontal",
        event_mode="both",
        final_value="75",
    )
    step = SkillStep(
        index=0,
        action="slider_set",
        fingerprint=ElementFingerprint(test_id="input-priority"),
        slider_set=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0].slider_set
    assert rs is not None
    assert rs.value_param == "priority"
    assert rs.min == 0.0
    assert rs.max == 100.0
    assert rs.step == 5.0
    assert rs.event_mode == "both"
    assert rs.final_value == "75"


# ----- detector ------------------------------------------------------------


def test_detector_collapses_drag_burst_into_one_slider_set_cluster() -> None:
    """A 4-event drag burst on the same range_slider produces ONE
    slider_set cluster anchored on the LAST event."""
    events = _assign_synthetic_ids([
        _slider_change("e1", "30", 1),
        _slider_change("e2", "40", 2),
        _slider_change("e3", "60", 3),
        _slider_change("e4", "75", 4, raw_event_kind="change"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_slider_set_clusters(events, causality, consumed)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "slider_set"
    assert c.primary_target_event_id == "e4"
    assert c.raw_event_ids == ["e1", "e2", "e3", "e4"]
    # All four events should be consumed so the fallback loop doesn't
    # also emit single_event clusters for them.
    assert all(eid in consumed for eid in ("e1", "e2", "e3", "e4"))


def test_detector_breaks_run_on_foreign_event() -> None:
    """A non-slider event between two slider events breaks the run.

    Two clusters should result: one for the first slider burst, one
    for the second."""
    foreign_click = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(test_id="btn-other"),
        page_url="http://x",
        event_id="middle",
        sequence=3,
        source="user_click",
    )
    events = _assign_synthetic_ids([
        _slider_change("e1", "20", 1),
        _slider_change("e2", "30", 2),
        foreign_click,
        _slider_change("e3", "50", 4),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_slider_set_clusters(events, causality, consumed)
    assert len(clusters) == 2
    assert clusters[0].raw_event_ids == ["e1", "e2"]
    assert clusters[1].raw_event_ids == ["e3"]


def test_detector_does_not_cluster_non_range_input() -> None:
    """A regular text input is NOT a slider_set."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id="input-title", tag="input",
            input_type="text", control_kind="text_input",
        ),
        value="hello",
        page_url="http://x",
        event_id="e1",
        sequence=1,
        source="user_change",
    )
    events = _assign_synthetic_ids([ev])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_slider_set_clusters(events, causality, consumed)
    assert clusters == []


# ----- annotator integration ----------------------------------------------


def test_build_skill_emits_one_slider_set_step_for_drag_burst() -> None:
    """A drag burst -> ONE slider_set step. The annotator's per-event
    loop must SKIP the non-primary burst events so we don't emit one
    change step per input event AND a slider_set step."""
    events = [
        _slider_change("e1", "30", 1),
        _slider_change("e2", "40", 2),
        _slider_change("e3", "60", 3),
        _slider_change("e4", "75", 4, raw_event_kind="change"),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    slider_steps = [s for s in skill.steps if s.action == "slider_set"]
    assert len(slider_steps) == 1
    s = slider_steps[0]
    assert s.slider_set is not None
    assert s.slider_set.final_value == "75"
    # And no leftover ``change`` steps from the burst.
    change_steps = [s for s in skill.steps if s.action == "change"]
    assert len(change_steps) == 0


def test_build_skill_declares_number_range_param_with_constraints() -> None:
    """The slider's HTML min/max should become ParamConstraints(min, max)
    on the declared SkillParam."""
    events = [
        _slider_change("e1", "75", 1, raw_event_kind="change"),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    # One slider_set step + one number_range param.
    slider_steps = [s for s in skill.steps if s.action == "slider_set"]
    assert len(slider_steps) == 1
    spec = slider_steps[0].slider_set
    assert spec is not None
    pname = spec.value_param
    params_by_name = {p.name: p for p in skill.params}
    assert pname in params_by_name
    p = params_by_name[pname]
    assert p.type == "number_range"
    assert p.constraints is not None
    assert p.constraints.min == 0.0
    assert p.constraints.max == 100.0


# ----- runner --------------------------------------------------------------


def test_runner_no_longer_treats_slider_set_as_unimplemented() -> None:
    assert "slider_set" not in _UNIMPLEMENTED_ACTIONS
