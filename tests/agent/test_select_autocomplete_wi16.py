"""WI-16: select_autocomplete (search + result selection).

Plan acceptance check:
  Search 'A-90' + pick A-9003 at recording; replay can pick A-9002
  with a different selected_item without re-recording.

Tests:
  - Schema roundtrip: AutocompleteSpec persists in skill JSON.
  - Detector: input burst + search request + result click ->
    ONE select_autocomplete cluster.
  - Annotator: build_skill emits ONE select_autocomplete step;
    declared params include BOTH query (from typed value) AND
    selected_item (from the result click).
  - Negative: input burst + non-result click stays as separate steps.
  - Runner stub: select_autocomplete no longer in unimplemented set.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_select_autocomplete_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    AutocompleteSpec,
    ElementFingerprint,
    NetworkExpectation,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS


def _input(event_id: str, value: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id="input-catalog-search", tag="input"
        ),
        value=value,
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=int(event_id[1:]) if event_id[1:].isdigit() else 0,
        source="user_input",
    )


def _result_request(event_id: str, caused_by: str | None = None) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="network_request",
        page_url="http://x/catalog",
        event_id=event_id,
        caused_by=caused_by,
        interaction_id=caused_by or event_id,
        sequence=20,
        request_id=event_id,
        method="GET",
        url="/api/search?q=A-9",
        source="fetch_request_start",
    )


def _result_response(event_id: str, caused_by: str | None = None) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="network_response",
        page_url="http://x/catalog",
        event_id=event_id,
        caused_by=caused_by,
        sequence=21,
        request_id="r1",
        method="GET",
        url="/api/search?q=A-9",
        status=200,
        source="fetch_response",
    )


def _result_click(
    event_id: str, asset_id: str = "A-9003"
) -> TraceEvent:
    """A click on a result row inside the catalog search results."""
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=f"btn-open-{asset_id}",
            tag="button",
            role="option",
            text=asset_id,
        ),
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=30,
        source="user_click",
    )


def _non_result_click(event_id: str) -> TraceEvent:
    """A click on a button that is NOT a result row."""
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id="btn-settings", tag="button", role="button"
        ),
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=30,
        source="user_click",
    )


# ----- schema --------------------------------------------------------------


def test_autocomplete_spec_roundtrip() -> None:
    spec = AutocompleteSpec(
        query_param="query",
        selected_item_param="selected_item",
        option_identity_template=ElementFingerprint(
            test_id="btn-open-{selected_item}",
        ),
        network_expectation=NetworkExpectation(
            url_pattern="/api/search", method="GET"
        ),
    )
    step = SkillStep(
        index=0,
        action="select_autocomplete",
        fingerprint=ElementFingerprint(test_id="input-catalog-search"),
        select_autocomplete=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0]
    assert rs.select_autocomplete is not None
    assert rs.select_autocomplete.query_param == "query"
    assert rs.select_autocomplete.selected_item_param == "selected_item"
    assert rs.select_autocomplete.network_expectation is not None
    assert (
        rs.select_autocomplete.network_expectation.url_pattern
        == "/api/search"
    )


# ----- detector -------------------------------------------------------------


def test_detector_collapses_input_burst_plus_result_click() -> None:
    """Input burst + a request + a result-row click -> ONE
    select_autocomplete cluster."""
    events = _assign_synthetic_ids([
        _input("i1", "A"),
        _input("i2", "A-"),
        _input("i3", "A-9"),
        _result_request("r1"),
        _result_response("r2"),
        _result_click("c1", asset_id="A-9003"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_select_autocomplete_clusters(events, causality, consumed)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "select_autocomplete"
    assert c.primary_target_event_id == "c1"
    # All burst inputs + observed children + the click should be in
    # the raw_event_ids audit trail.
    assert {"i1", "i2", "i3", "r1", "r2", "c1"}.issubset(set(c.raw_event_ids))
    assert {"i1", "i2", "i3", "r1", "r2", "c1"}.issubset(consumed)


def test_detector_does_not_collapse_non_result_click() -> None:
    """Negative: a click on something that doesn't look like a result
    row (no option role, no result/row test_id) does NOT collapse into
    an autocomplete cluster."""
    events = _assign_synthetic_ids([
        _input("i1", "foo"),
        _non_result_click("c1"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_select_autocomplete_clusters(events, causality, consumed)
    assert clusters == []
    assert consumed == set()


# ----- annotator integration ----------------------------------------------


def test_build_skill_emits_one_select_autocomplete_step() -> None:
    events = [
        _input("i1", "A"),
        _input("i2", "A-9"),
        _result_request("r1"),
        _result_response("r2"),
        _result_click("c1", asset_id="A-9003"),
    ]
    skill = build_skill(skill_name="search", events=events, auto=True)
    # Exactly one step.
    autocomplete_steps = [
        s for s in skill.steps if s.action == "select_autocomplete"
    ]
    assert len(autocomplete_steps) == 1
    s = autocomplete_steps[0]
    assert s.select_autocomplete is not None
    spec = s.select_autocomplete
    assert spec.query_param == "query"
    assert spec.selected_item_param  # bound to the click's binding name
    # Provenance audit carries all the raw event ids.
    assert s.provenance is not None
    assert {"i1", "i2", "c1"}.issubset(set(s.provenance.raw_event_ids))


def test_build_skill_declares_query_and_selected_item_params() -> None:
    """Plan acceptance: separate ``query`` param from ``selected_item``
    so the operator can vary the pick without changing the query."""
    events = [
        _input("i1", "A-9"),
        _result_request("r1"),
        _result_click("c1", asset_id="A-9003"),
    ]
    skill = build_skill(skill_name="search", events=events, auto=True)
    param_names = {p.name for p in skill.params}
    # query param must exist from the typed-in value.
    assert "query" in param_names
    # selected_item param must exist (the click's binding name, derived
    # from the click target's test_id 'btn-open-A-9003' -> something
    # like 'btn_open_a_9003' through the legacy naming heuristic).
    assert any(name != "query" for name in param_names)


# ----- runner --------------------------------------------------------------


def test_runner_no_longer_treats_select_autocomplete_as_unimplemented() -> None:
    assert "select_autocomplete" not in _UNIMPLEMENTED_ACTIONS
