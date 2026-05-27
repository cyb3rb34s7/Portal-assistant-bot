"""WI-15: fill_submit semantic cluster + step + runner dispatch.

Plan acceptance check:
  Catalog search 'A-9003' emits ONE fill_submit step with the final
  value, not 4 fills + Enter + submit.

Tests:
  - Schema roundtrip: FillSubmitSpec persists in skill JSON.
  - Detector: contiguous input_change burst + Enter -> ONE
    fill_submit cluster; the intermediate inputs are folded in.
  - Annotator: build_skill emits ONE fill_submit step (not 4 changes
    + a key step); param_binding carries the final value.
  - Negative: a typing burst NOT followed by a submit trigger stays
    as separate change steps (no over-clustering).
  - Negative: button click without submit-y text DOES NOT fold into
    fill_submit (avoids fold-the-next-step bug).
  - Runner stub: a synthetic FillSubmit step routes to _do_fill_submit
    (no longer falls through to _do_unimplemented_action).
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_fill_submit_clusters,
    build_causality_graph,
    build_skill,
    detect_semantic_clusters,
)
from pilot.skill_models import (
    ElementFingerprint,
    FillSubmitSpec,
    SemanticCluster,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS


# ----- helpers -------------------------------------------------------------


def _input(event_id: str, value: str, target: str = "input-catalog-search") -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(test_id=target, tag="input"),
        value=value,
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=int(event_id[1:]) if event_id[1:].isdigit() else 0,
        source="user_input",
    )


def _key_enter(event_id: str, target: str = "input-catalog-search") -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="key",
        fingerprint=ElementFingerprint(test_id=target, tag="input"),
        value="Enter",
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=10,
        source="user_keydown",
    )


def _submit_button_click(event_id: str, text: str = "Search") -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id="btn-search",
            tag="button",
            accessible_name=text,
            text=text,
            control_kind="button",
            role="button",
        ),
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=10,
        source="user_click",
    )


def _click_other(event_id: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id="btn-something-else",
            tag="button",
            accessible_name="Something Else",
            text="Something Else",
            role="button",
        ),
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=10,
        source="user_click",
    )


# ----- schema roundtrip ----------------------------------------------------


def test_fill_submit_spec_roundtrip() -> None:
    spec = FillSubmitSpec(
        submit_trigger="enter",
        value_param="query",
        expected_result_signal="/api/search",
    )
    step = SkillStep(
        index=0,
        action="fill_submit",
        fingerprint=ElementFingerprint(test_id="input"),
        fill_submit=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0]
    assert rs.action == "fill_submit"
    assert rs.fill_submit is not None
    assert rs.fill_submit.submit_trigger == "enter"
    assert rs.fill_submit.value_param == "query"
    assert rs.fill_submit.expected_result_signal == "/api/search"


# ----- detector unit tests -------------------------------------------------


def test_detector_collapses_input_burst_plus_enter() -> None:
    """4 input_changes on the same field + Enter -> ONE fill_submit
    cluster carrying all 5 event ids; intermediate inputs are folded."""
    events = _assign_synthetic_ids([
        _input("i1", "A"),
        _input("i2", "A-"),
        _input("i3", "A-9"),
        _input("i4", "A-9003"),
        _key_enter("k1"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_fill_submit_clusters(events, causality, consumed)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "fill_submit"
    assert c.primary_target_event_id == "i4"  # last input carries final value
    assert set(c.raw_event_ids) == {"i1", "i2", "i3", "i4", "k1"}
    # All 5 events are marked consumed so the single_event fallback
    # doesn't double-emit them.
    assert {"i1", "i2", "i3", "i4", "k1"}.issubset(consumed)


def test_detector_collapses_burst_plus_submit_button_click() -> None:
    """Input burst + a click on a Search button -> ONE fill_submit
    cluster with submit_trigger='button' detection."""
    events = _assign_synthetic_ids([
        _input("i1", "foo"),
        _submit_button_click("c1", text="Search"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_fill_submit_clusters(events, causality, consumed)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "fill_submit"
    assert "c1" in clusters[0].raw_event_ids


def test_detector_does_not_fold_non_submit_click_into_fill_submit() -> None:
    """Negative: a click on a button whose text isn't submit-verb stays
    as its own step. This is the 'do not fold the next step' guard."""
    events = _assign_synthetic_ids([
        _input("i1", "foo"),
        _click_other("c1"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_fill_submit_clusters(events, causality, consumed)
    assert clusters == []  # No collapse; ordinary click stays separate
    assert consumed == set()  # Nothing consumed


def test_detector_burst_without_trigger_is_not_clustered() -> None:
    """A standalone typing burst (no Enter / no submit click / no
    submit event) is NOT a fill_submit. The detector returns empty
    and the per-event fallback handles each input_change normally."""
    events = _assign_synthetic_ids([
        _input("i1", "abc"),
        _input("i2", "abcd"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_fill_submit_clusters(events, causality, consumed)
    assert clusters == []
    assert consumed == set()


# ----- annotator integration (build_skill) ---------------------------------


def test_build_skill_emits_one_fill_submit_step_for_burst_plus_enter() -> None:
    """Plan acceptance: catalog search 'A-9003' emits ONE fill_submit
    step with the final value, not 4 fills + Enter + submit."""
    events = [
        _input("i1", "A"),
        _input("i2", "A-"),
        _input("i3", "A-9"),
        _input("i4", "A-9003"),
        _key_enter("k1"),
    ]
    skill = build_skill(skill_name="search", events=events, auto=True)
    # Exactly one step.
    assert len(skill.steps) == 1
    s = skill.steps[0]
    assert s.action == "fill_submit"
    assert s.fill_submit is not None
    assert s.fill_submit.submit_trigger == "enter"
    # The step's value carries the FINAL value (from the primary target = i4).
    assert s.value == "A-9003"
    # A param was declared from the binding.
    assert s.param_binding is not None
    # Provenance carries all event ids for audit.
    assert s.provenance is not None
    assert set(s.provenance.raw_event_ids) == {"i1", "i2", "i3", "i4", "k1"}
    assert s.provenance.cluster_kind == "fill_submit"


def test_build_skill_keeps_linear_path_when_burst_has_no_trigger() -> None:
    """Negative: a burst alone (no Enter, no submit click) keeps the
    legacy per-event emission. Each input_change becomes a change step."""
    events = [
        _input("i1", "abc"),
        _input("i2", "abcd"),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    # No fill_submit collapse -> per-event emission (one change step
    # per input_change). The annotate-loop's noise filter may dedupe
    # adjacent same-field changes to the last one; either way it's
    # NOT a fill_submit step.
    assert all(s.action != "fill_submit" for s in skill.steps)
    assert all(s.fill_submit is None for s in skill.steps)


# ----- runner dispatch -----------------------------------------------------


def test_runner_no_longer_treats_fill_submit_as_unimplemented() -> None:
    """fill_submit was moved out of _UNIMPLEMENTED_ACTIONS in WI-15."""
    assert "fill_submit" not in _UNIMPLEMENTED_ACTIONS
