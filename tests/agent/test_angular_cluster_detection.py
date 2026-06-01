"""2026-06-02 batch 3: Angular cluster detection.

Pins the annotator's detection of Angular Material / ng-bootstrap widget
patterns into the right semantic clusters:

  - mat-select panel-open click + mat-option pick -> ONE select_option
    cluster with recorded_label, known_options, match_mode='label', and
    require_search=False (no search box in the panel by default).

  - ng-multiselect-dropdown open click + search input_change + option-row
    click + outside close -> ONE set_selection cluster with
    require_search=True, search_fp populated, item_labels populated.

  - cdk-virtual-scroll-viewport + sibling search-input + mat-checkbox row
    clicks -> ONE set_selection cluster with require_search=True.

  - Labeled-widget param pass does NOT emit a stray param for the
    cluster's anchor click (no double-binding).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pilot.annotate import (
    _assign_synthetic_ids,
    _build_select_option_spec,
    _build_set_selection_spec,
    _detect_select_option_clusters,
    _detect_set_selection_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    OptionSnapshot,
    TraceEvent,
)


# ---- helpers ---------------------------------------------------------------


def _mat_select_open_click(
    event_id: str,
    *,
    elt_id: str,
    label: str,
    current_value: Optional[str],
    options_seen: list[tuple[str, str]] | None = None,
    sequence: int,
) -> TraceEvent:
    fp = ElementFingerprint(
        tag="mat-select",
        role="listbox",
        element_id=elt_id,
        accessible_name=label,
        text=current_value,
        current_value=current_value,
    )
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=fp,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_click",
    )
    if options_seen:
        ev.options_seen = [
            OptionSnapshot(value=v, label=lbl) for v, lbl in options_seen
        ]
    return ev


def _mat_option_click(
    event_id: str,
    *,
    opt_id: str,
    opt_label: str,
    sequence: int,
) -> TraceEvent:
    fp = ElementFingerprint(
        tag="mat-option",
        role="option",
        element_id=opt_id,
        text=opt_label,
        accessible_name=opt_label,
        css_path=f"mat-option#{opt_id}",
    )
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=fp,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _ng_multiselect_click(
    event_id: str,
    *,
    label: str,
    current_value: Optional[str],
    sequence: int,
    text: Optional[str] = None,
) -> TraceEvent:
    fp = ElementFingerprint(
        tag="ng-multiselect-dropdown",
        accessible_name=label,
        text=text or current_value or "Choose",
        current_value=current_value,
        css_path="app-root > div > main > ng-multiselect-dropdown",
    )
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=fp,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _ng_multiselect_search_input(
    event_id: str,
    *,
    value: str,
    sequence: int,
) -> TraceEvent:
    fp = ElementFingerprint(
        tag="input",
        placeholder="Search",
        aria_label="multiselect-search",
        accessible_name="multiselect-search",
        input_type="text",
        ancestor_chain=[
            {"tag": "li", "className": "filter-textbox"},
            {"tag": "ul", "className": "item1"},
            {"tag": "div", "className": "dropdown-list"},
            {"tag": "div", "className": "multiselect-dropdown"},
            {"tag": "ng-multiselect-dropdown", "className": "ng-untouched"},
        ],
    )
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=fp,
        value=value,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_input",
    )


def _ng_multiselect_option_row_click(
    event_id: str,
    *,
    label: str,
    sequence: int,
) -> TraceEvent:
    fp = ElementFingerprint(
        tag="li",
        accessible_name=label,
        text=label,
        css_path="ng-multiselect-dropdown ul.item2 li",
        ancestor_chain=[
            {"tag": "ul", "className": "item2"},
            {"tag": "div", "className": "dropdown-list"},
            {"tag": "div", "className": "multiselect-dropdown"},
            {"tag": "ng-multiselect-dropdown", "className": "ng-untouched"},
        ],
    )
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=fp,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _cdk_search_input(
    event_id: str,
    *,
    value: str,
    sequence: int,
    test_id: str = "left-search",
) -> TraceEvent:
    fp = ElementFingerprint(
        tag="input",
        test_id=test_id,
        placeholder="Search",
        input_type="text",
        ancestor_chain=[
            {"tag": "div", "className": "mat-form-field-infix"},
            {"tag": "mat-form-field"},
            {"tag": "div", "className": "ccMapWLft", "id": "left-panel"},
        ],
    )
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=fp,
        value=value,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_input",
    )


def _cdk_row_checkbox_click(
    event_id: str,
    *,
    label: str,
    mc_id: str,
    sequence: int,
) -> TraceEvent:
    fp = ElementFingerprint(
        tag="mat-checkbox",
        element_id=mc_id,
        accessible_name=label,
        text=label,
        role="checkbox",
        ancestor_chain=[
            {"tag": "div", "className": "cdkRow"},
            {"tag": "div", "className": "cdk-virtual-scroll-content-wrapper"},
            {"tag": "cdk-virtual-scroll-viewport", "className": "cdkWrap"},
            {"tag": "div", "className": "ccMapWLft", "id": "left-panel"},
        ],
    )
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=fp,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_click",
    )


# ---- A. mat-select + mat-option -> select_option ---------------------------


def test_mat_select_panel_pick_collapses_to_select_option() -> None:
    """A click on a mat-select widget root followed by a click on a
    mat-option inside the panel -> ONE select_option cluster."""
    events = _assign_synthetic_ids([
        _mat_select_open_click(
            "e_open",
            elt_id="mat-select-year",
            label="Year*:",
            current_value="2024",
            options_seen=[
                ("mat-option-0", "2026"),
                ("mat-option-1", "2025"),
                ("mat-option-2", "2024"),
                ("mat-option-3", "2023"),
            ],
            sequence=1,
        ),
        _mat_option_click(
            "e_pick", opt_id="mat-option-3", opt_label="2023", sequence=2
        ),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_select_option_clusters(events, causality, consumed)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "select_option"
    # Both events folded into the cluster.
    assert {"e_open", "e_pick"}.issubset(set(c.raw_event_ids))
    # The OPTION click is the primary target.
    assert c.primary_target_event_id == "e_pick"

    # End-to-end through build_skill -- the select_option step carries
    # the spec with recorded_label, known_options, no require_search.
    skill = build_skill(skill_name="t", events=events, auto=True)
    so_steps = [s for s in skill.steps if s.action == "select_option"]
    assert len(so_steps) == 1
    spec = so_steps[0].select_option
    assert spec is not None
    assert spec.recorded_label == "2023"
    assert spec.recorded_value == "mat-option-3"
    labels = {o.label for o in spec.known_options}
    assert {"2026", "2025", "2024", "2023"}.issubset(labels)
    assert spec.require_search is False
    assert spec.match_mode == "label"


def test_mat_select_open_alone_does_not_collapse() -> None:
    """A bare mat-select panel-open click with NO follow-up option click
    does NOT collapse into a select_option cluster. The labeled-widget
    pass will still surface it as a param."""
    events = _assign_synthetic_ids([
        _mat_select_open_click(
            "e_open",
            elt_id="mat-select-year",
            label="Year*:",
            current_value="2024",
            options_seen=[("mat-option-0", "2024")],
            sequence=1,
        ),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_select_option_clusters(events, causality, consumed)
    assert clusters == []


def test_mat_select_no_double_binding_in_labeled_widget_pass() -> None:
    """When a mat-select+mat-option click is collapsed into a
    select_option step, the labeled-widget pass MUST NOT emit a
    duplicate ``year`` boolean/string param for the open click."""
    events = _assign_synthetic_ids([
        _mat_select_open_click(
            "e_open",
            elt_id="mat-select-year",
            label="Year*:",
            current_value="2024",
            options_seen=[
                ("mat-option-0", "2026"),
                ("mat-option-1", "2025"),
                ("mat-option-2", "2024"),
            ],
            sequence=1,
        ),
        _mat_option_click(
            "e_pick", opt_id="mat-option-2", opt_label="2024", sequence=2
        ),
    ])
    skill = build_skill(skill_name="t", events=events, auto=True)
    # Param count for 'year' is exactly one.
    year_params = [p for p in skill.params if p.name == "year"]
    assert len(year_params) == 1


# ---- B. ng-multiselect-dropdown -> set_selection ---------------------------


def test_ng_multiselect_open_search_pick_close_collapses_to_set_selection() -> None:
    """ng-multiselect-dropdown open + search input + option row click +
    outside close -> ONE set_selection cluster with require_search=True
    and a populated search_fp + item_labels."""
    events = _assign_synthetic_ids([
        _ng_multiselect_click(
            "e_open",
            label="Country/Region*:",
            current_value="Choose Country/Region",
            sequence=1,
        ),
        _ng_multiselect_search_input(
            "e_search", value="canada", sequence=2,
        ),
        _ng_multiselect_option_row_click(
            "e_pick", label="Canada", sequence=3,
        ),
        # Close click on the picker root.
        _ng_multiselect_click(
            "e_close",
            label="Country/Region*:",
            current_value="Canada",
            text="Canada x",
            sequence=4,
        ),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_set_selection_clusters(events, causality, consumed)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.cluster_kind == "set_selection"
    assert "e_pick" in cluster.raw_event_ids

    # End-to-end: the skill emits a set_selection step with
    # require_search=True, search_fp non-None, item_labels populated.
    skill = build_skill(skill_name="t", events=events, auto=True)
    ss_steps = [s for s in skill.steps if s.action == "set_selection"]
    assert len(ss_steps) == 1
    spec = ss_steps[0].set_selection
    assert spec is not None
    assert spec.require_search is True
    assert spec.search_fp is not None
    assert "Canada" in spec.item_labels.values()


def test_ng_multiselect_no_double_binding() -> None:
    """When ng-multiselect cluster collapses, the labeled-widget pass
    MUST NOT emit a stray ``country_region`` widget param for the
    open click."""
    events = _assign_synthetic_ids([
        _ng_multiselect_click(
            "e_open",
            label="Country/Region*:",
            current_value="Choose Country/Region",
            sequence=1,
        ),
        _ng_multiselect_search_input(
            "e_search", value="canada", sequence=2,
        ),
        _ng_multiselect_option_row_click(
            "e_pick", label="Canada", sequence=3,
        ),
        _ng_multiselect_click(
            "e_close",
            label="Country/Region*:",
            current_value="Canada",
            text="Canada x",
            sequence=4,
        ),
    ])
    skill = build_skill(skill_name="t", events=events, auto=True)
    # Exactly one param is emitted for the country picker (from the
    # set_selection spec). The labeled-widget pass MUST NOT emit a
    # second one.
    cr_params = [
        p for p in skill.params
        if p.name in ("country_region", "country")
    ]
    assert len(cr_params) == 1


# ---- C. cdk-virtual-scroll + sibling search -> set_selection ---------------


def test_cdk_viewport_with_sibling_search_collapses_to_set_selection() -> None:
    """cdk-virtual-scroll viewport with a sibling search input and
    mat-checkbox row clicks -> ONE set_selection cluster with
    require_search=True."""
    events = _assign_synthetic_ids([
        # Operator typed in the sibling search above the viewport.
        _cdk_search_input("e_search", value="frame", sequence=1),
        _cdk_row_checkbox_click(
            "e_row1", label="FRAME_24_BOMRB_8K_GIT09", mc_id="mat-checkbox-left-0",
            sequence=2,
        ),
        _cdk_row_checkbox_click(
            "e_row2", label="FRAME_24_BOMRB_8K_GIT10", mc_id="mat-checkbox-left-1",
            sequence=3,
        ),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_set_selection_clusters(events, causality, consumed)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.cluster_kind == "set_selection"
    assert "e_row1" in cluster.raw_event_ids
    assert "e_row2" in cluster.raw_event_ids
    assert "e_search" in cluster.raw_event_ids

    # End-to-end: require_search=True, two items in item_labels.
    skill = build_skill(skill_name="t", events=events, auto=True)
    ss_steps = [s for s in skill.steps if s.action == "set_selection"]
    assert len(ss_steps) == 1
    spec = ss_steps[0].set_selection
    assert spec is not None
    assert spec.require_search is True
    assert spec.search_fp is not None
    assert "FRAME_24_BOMRB_8K_GIT09" in spec.item_labels.values()
    assert "FRAME_24_BOMRB_8K_GIT10" in spec.item_labels.values()


def test_cdk_no_double_binding() -> None:
    """The labeled-widget pass MUST NOT emit a stray widget param for
    the first cdk-row checkbox click when it's the cluster anchor."""
    events = _assign_synthetic_ids([
        _cdk_search_input("e_search", value="frame", sequence=1),
        _cdk_row_checkbox_click(
            "e_row1", label="FRAME_24_BOMRB_8K_GIT09", mc_id="mat-checkbox-left-0",
            sequence=2,
        ),
    ])
    skill = build_skill(skill_name="t", events=events, auto=True)
    # No labeled-widget-derived ``frame_24_bomrb_8k_git09`` param.
    bad = [p for p in skill.params if "frame_24" in p.name.lower()]
    assert bad == []
