"""WI-20: nested/searchable/dependent multi-select.

Plan acceptance check:
  Nested picker with a different parent at replay selects only options
  valid under the new parent.

Tests:
  - Schema roundtrip: SetSelectionSpec extension fields persist
    (depends_on, parent_picker_fp, option_source, search_result_signal,
     hierarchy_path).
  - Detector: parent_select change -> network_request -> multiselect
    burst yields a (parent_event, request_event) tuple on the child
    cluster's primary_target.
  - Annotator: build_skill stamps depends_on + option_source on the
    child set_selection step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_dependent_multiselect,
    _detect_set_selection_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    NetworkExpectation,
    OptionSnapshot,
    SetSelectionSpec,
    Skill,
    SkillStep,
    TraceEvent,
)


def _select_change(event_id: str, test_id: str, name_attr: str, value: str,
                   options: list[dict[str, Any]], sequence: int = 1) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id=test_id, name=name_attr, tag="select",
            control_kind="select_single",
            options_snapshot=[
                OptionSnapshot(value=o["value"], label=o["label"])
                for o in options
            ],
            selected_options=[value],
        ),
        value=value,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_change",
    )


def _request(event_id: str, caused_by: str, url: str, sequence: int = 2) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="network_request",
        page_url="http://x",
        event_id=event_id,
        caused_by=caused_by,
        interaction_id=caused_by,
        sequence=sequence,
        request_id=event_id,
        method="GET",
        url=url,
        source="fetch_request_start",
    )


def _click(event_id: str, test_id: str, sequence: int = 1) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id, tag="button", role="button"
        ),
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_click",
    )


# ----- schema --------------------------------------------------------------


def test_set_selection_spec_dependency_fields_roundtrip() -> None:
    spec = SetSelectionSpec(
        mode="replace",
        param="subcategories",
        depends_on="category",
        parent_picker_fp=ElementFingerprint(test_id="select-category"),
        option_source=NetworkExpectation(
            url_pattern="/api/subcategories", method="GET"
        ),
        search_result_signal="/api/subcategory-search",
        hierarchy_path=["category", "subcategories"],
    )
    step = SkillStep(
        index=0, action="set_selection",
        fingerprint=ElementFingerprint(test_id="x"),
        set_selection=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0].set_selection
    assert rs.depends_on == "category"
    assert rs.parent_picker_fp is not None
    assert rs.parent_picker_fp.test_id == "select-category"
    assert rs.option_source is not None
    assert rs.option_source.url_pattern == "/api/subcategories"
    assert rs.search_result_signal == "/api/subcategory-search"
    assert rs.hierarchy_path == ["category", "subcategories"]


# ----- detector ------------------------------------------------------------


def test_dependent_multiselect_detector_with_intervening_request() -> None:
    events = _assign_synthetic_ids([
        # Parent select change.
        _select_change(
            "p1", "select-category", "category", "tv",
            [{"value": "tv", "label": "TV"}, {"value": "movies", "label": "Movies"}],
            sequence=1,
        ),
        # Network refresh.
        _request("r1", "p1", "/api/subcategories?category=tv", sequence=2),
        # Child multiselect burst.
        _click("c1", "multiselect-subcategories-toggle", sequence=3),
        _click("c2", "multiselect-subcategories-checkbox-comedy", sequence=4),
        _click("c3", "multiselect-subcategories-toggle", sequence=5),
    ])
    causality = build_causality_graph(events)
    set_clusters = _detect_set_selection_clusters(events, causality, set())
    assert len(set_clusters) == 1
    dependents = _detect_dependent_multiselect(events, causality, set_clusters)
    assert len(dependents) == 1
    # Mapping key is the child cluster's primary_target_event_id.
    primary = set_clusters[0].primary_target_event_id
    assert primary in dependents
    parent_ev, req_ev = dependents[primary]
    assert parent_ev is not None
    assert parent_ev.event_id == "p1"
    assert req_ev is not None
    assert req_ev.event_id == "r1"


# ----- annotator integration ----------------------------------------------


def test_build_skill_stamps_depends_on_and_option_source_on_child() -> None:
    events = [
        _select_change(
            "p1", "select-category", "category", "tv",
            [{"value": "tv", "label": "TV"}],
            sequence=1,
        ),
        _request("r1", "p1", "/api/subcategories?category=tv", sequence=2),
        _click("c1", "multiselect-subcategories-toggle", sequence=3),
        _click("c2", "multiselect-subcategories-checkbox-comedy", sequence=4),
        _click("c3", "multiselect-subcategories-toggle", sequence=5),
    ]
    skill = build_skill(skill_name="nested", events=events, auto=True)
    ss = [s for s in skill.steps if s.action == "set_selection"]
    assert len(ss) == 1
    spec = ss[0].set_selection
    assert spec is not None
    assert spec.depends_on == "category"
    assert spec.parent_picker_fp is not None
    assert spec.parent_picker_fp.test_id == "select-category"
    assert spec.option_source is not None
    assert spec.option_source.url_pattern == "/api/subcategories"
    # hierarchy_path has two entries (parent -> child).
    assert len(spec.hierarchy_path) == 2
    assert spec.hierarchy_path[0] == "category"
    assert spec.hierarchy_path[1] == "subcategories"
