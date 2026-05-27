"""WI-21: native and custom date picker semantics.

Plan acceptance check:
  Recorded date 2026-05-20 replays a different date without needing
  the same calendar nav clicks.

Tests:
  - Schema roundtrip: DatePickerSpec persists.
  - Detector: input_change on a date_input -> ONE date_select cluster
    (native).
  - Detector: click on a gridcell role -> ONE date_select cluster
    (custom).
  - Annotator: build_skill emits a date_select step with type=date
    param declared, codec=iso_date.
  - Runner stub: date_select no longer in unimplemented set.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_date_select_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    DatePickerSpec,
    ElementFingerprint,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS


def _native_date_change(
    event_id: str, value: str, test_id: str = "input-publish-at"
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="input",
            input_type="date",
            control_kind="date_input",
            value_kind="date",
        ),
        value=value,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=1,
        source="user_change",
    )


def _calendar_cell_click(event_id: str, day: int) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=f"calendar-day-{day}",
            tag="button",
            role="gridcell",
            text=str(day),
            accessible_name=str(day),
        ),
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=1,
        source="user_click",
    )


# ----- schema --------------------------------------------------------------


def test_date_picker_spec_roundtrip() -> None:
    spec = DatePickerSpec(
        kind="native",
        value_param="publish_at",
        timezone="America/Los_Angeles",
    )
    step = SkillStep(
        index=0, action="date_select",
        fingerprint=ElementFingerprint(test_id="input-publish-at"),
        date_select=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0].date_select
    assert rs is not None
    assert rs.kind == "native"
    assert rs.value_param == "publish_at"
    assert rs.timezone == "America/Los_Angeles"


# ----- detector ------------------------------------------------------------


def test_detector_emits_native_date_cluster_for_date_input() -> None:
    events = _assign_synthetic_ids([
        _native_date_change("e1", "2026-05-20"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_date_select_clusters(events, causality, consumed)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "date_select"
    assert clusters[0].primary_target_event_id == "e1"
    assert "e1" in consumed


def test_detector_emits_custom_date_cluster_for_gridcell_click() -> None:
    events = _assign_synthetic_ids([
        _calendar_cell_click("e1", 20),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_date_select_clusters(events, causality, consumed)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "date_select"
    assert clusters[0].primary_target_event_id == "e1"


def test_detector_does_not_cluster_non_date_input() -> None:
    """Negative: a text input_change is NOT a date_select."""
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
    )
    events = _assign_synthetic_ids([ev])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_date_select_clusters(events, causality, consumed)
    assert clusters == []


# ----- annotator integration ----------------------------------------------


def test_build_skill_emits_date_select_step_with_iso_date_codec() -> None:
    """Native date_input -> ONE date_select step + a SkillParam typed
    'date' with codec 'iso_date'."""
    events = [
        _native_date_change("e1", "2026-05-20", test_id="input-publish-at"),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    ds = [s for s in skill.steps if s.action == "date_select"]
    assert len(ds) == 1
    s = ds[0]
    assert s.date_select is not None
    assert s.date_select.kind == "native"
    # Param declared with type=date + codec=iso_date.
    params_by_name = {p.name: p for p in skill.params}
    assert s.date_select.value_param in params_by_name
    p = params_by_name[s.date_select.value_param]
    assert p.type == "date"
    assert p.codec == "iso_date"


def test_build_skill_emits_custom_date_select_for_calendar_click() -> None:
    events = [
        _calendar_cell_click("e1", 20),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    ds = [s for s in skill.steps if s.action == "date_select"]
    assert len(ds) == 1
    s = ds[0]
    assert s.date_select is not None
    assert s.date_select.kind == "custom"
    # day_cell_template_fp is set so the runner can navigate by day.
    assert s.date_select.day_cell_template_fp is not None
    # selected_day_identity is stored for audit, NOT for replay.
    assert s.date_select.selected_day_identity is not None


# ----- runner --------------------------------------------------------------


def test_runner_no_longer_treats_date_select_as_unimplemented() -> None:
    assert "date_select" not in _UNIMPLEMENTED_ACTIONS
