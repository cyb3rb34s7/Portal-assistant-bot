"""WI-19: custom multi-select auto-collapse to set_selection.

Plan acceptance check:
  Recording categories [sports, drama] replayed with [kids] ends with
  exactly [kids] chips (final_equality_assertion verified).

Tests:
  - Schema: final_equality_assertion default True.
  - Detector: toggle + search + checkbox + checkbox + toggle ->
    ONE set_selection cluster.
  - Annotator: build_skill emits ONE set_selection step with the
    list param declared (string_list).
  - Negative: a lone toggle click (no checkboxes) is NOT clustered.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_set_selection_clusters,
    _multiselect_prefix,
    _multiselect_role,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    SetSelectionSpec,
    Skill,
    SkillStep,
    TraceEvent,
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


def _input(event_id: str, test_id: str, value: str, sequence: int = 1) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(test_id=test_id, tag="input"),
        value=value,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_input",
    )


# ----- helpers -------------------------------------------------------------


def test_multiselect_prefix_recognizes_widget_testids() -> None:
    assert _multiselect_prefix(
        ElementFingerprint(test_id="multiselect-categories-toggle")
    ) == "multiselect-categories"
    assert _multiselect_prefix(
        ElementFingerprint(test_id="multiselect-categories-search")
    ) == "multiselect-categories"
    assert _multiselect_prefix(
        ElementFingerprint(test_id="multiselect-categories-checkbox-sports")
    ) == "multiselect-categories"
    assert _multiselect_prefix(
        ElementFingerprint(test_id="multiselect-categories-item-drama")
    ) == "multiselect-categories"
    assert _multiselect_prefix(
        ElementFingerprint(test_id="multiselect-categories-chip-sports")
    ) == "multiselect-categories"


def test_multiselect_role_classification() -> None:
    assert _multiselect_role(
        ElementFingerprint(test_id="multiselect-categories-toggle")
    ) == "toggle"
    assert _multiselect_role(
        ElementFingerprint(test_id="multiselect-categories-search")
    ) == "search"
    assert _multiselect_role(
        ElementFingerprint(test_id="multiselect-categories-checkbox-sports")
    ) == "checkbox"


# ----- schema --------------------------------------------------------------


def test_set_selection_spec_default_final_equality_assertion_true() -> None:
    spec = SetSelectionSpec(mode="replace", param="categories")
    assert spec.final_equality_assertion is True
    # Persists in JSON.
    step = SkillStep(
        index=0, action="set_selection",
        fingerprint=ElementFingerprint(test_id="x"),
        set_selection=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    assert restored.steps[0].set_selection.final_equality_assertion is True


# ----- detector ------------------------------------------------------------


def test_detector_collapses_multi_select_burst() -> None:
    """toggle-open + search + checkbox + checkbox + toggle-close
    -> ONE set_selection cluster."""
    events = _assign_synthetic_ids([
        _click("c1", "multiselect-categories-toggle", sequence=1),
        _input("i1", "multiselect-categories-search", "spo", sequence=2),
        _click("c2", "multiselect-categories-checkbox-sports", sequence=3),
        _input("i2", "multiselect-categories-search", "dra", sequence=4),
        _click("c3", "multiselect-categories-checkbox-drama", sequence=5),
        _click("c4", "multiselect-categories-toggle", sequence=6),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_set_selection_clusters(events, causality, consumed)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "set_selection"
    # ALL widget events are folded.
    assert {"c1", "i1", "c2", "i2", "c3", "c4"}.issubset(set(c.raw_event_ids))
    assert {"c1", "i1", "c2", "i2", "c3", "c4"}.issubset(consumed)


def test_detector_skips_lone_toggle_click() -> None:
    """A toggle-open with no checkbox follow-ups is NOT a meaningful
    set_selection (operator peeked). Returns empty."""
    events = _assign_synthetic_ids([
        _click("c1", "multiselect-categories-toggle", sequence=1),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_set_selection_clusters(events, causality, consumed)
    assert clusters == []
    assert consumed == set()


def _mutation(event_id: str, sequence: int = 1) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="dom_mutation",
        fingerprint=None,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="observer",
    )


def test_detector_does_not_claim_accordion_toggle() -> None:
    """Regression (2026-05-28 e2e round, scenario 6): a NON-multiselect
    disclosure button whose testid happens to end in ``-toggle`` (e.g.
    ``accordion-advanced-toggle``) followed only by an observed DOM
    mutation (aria-expanded flip) must NOT be claimed as a set_selection.

    Before the fix, the observed event inflated the cluster past the
    ``len <= 1`` guard, producing a degenerate set_selection with no
    checkbox_template_fp + empty known_options that the runner could not
    replay (``set_selection: no checkbox_template_fp``), and it pre-empted
    toggle_state detection. The fix requires at least one checkbox/item
    selection event before consuming the cluster.
    """
    expand_fp = ElementFingerprint(
        test_id="accordion-advanced-toggle",
        tag="button",
        role="button",
        aria_expanded=False,
    )
    events = _assign_synthetic_ids([
        TraceEvent(
            ts=datetime.utcnow(),
            kind="click",
            fingerprint=expand_fp,
            page_url="http://x",
            event_id="c1",
            interaction_id="c1",
            caused_by=None,
            sequence=1,
            source="user_click",
        ),
        _mutation("m1", sequence=2),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_set_selection_clusters(events, causality, consumed)
    assert clusters == []
    # The accordion click stays AVAILABLE for toggle_state / single-event.
    assert "c1" not in consumed


# ----- annotator integration ----------------------------------------------


def test_build_skill_emits_one_set_selection_step() -> None:
    events = [
        _click("c1", "multiselect-categories-toggle", sequence=1),
        _click("c2", "multiselect-categories-checkbox-sports", sequence=2),
        _click("c3", "multiselect-categories-checkbox-drama", sequence=3),
        _click("c4", "multiselect-categories-toggle", sequence=4),
    ]
    skill = build_skill(skill_name="set_categories", events=events, auto=True)
    ss = [s for s in skill.steps if s.action == "set_selection"]
    assert len(ss) == 1
    s = ss[0]
    assert s.set_selection is not None
    spec = s.set_selection
    # WI-19 default: equality assertion enabled.
    assert spec.final_equality_assertion is True
    # Mode default for cluster-generated set_selection is replace.
    assert spec.mode == "replace"
    # Param declared with string_list type, example carries items.
    params_by_name = {p.name: p for p in skill.params}
    assert "categories" in params_by_name
    assert params_by_name["categories"].type == "string_list"
    # The recorded items show up in the example.
    assert (
        "sports" in (params_by_name["categories"].example or "")
        and "drama" in (params_by_name["categories"].example or "")
    )
