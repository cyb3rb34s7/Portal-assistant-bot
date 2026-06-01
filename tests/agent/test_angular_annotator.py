"""2026-06-02 batch 2: Angular-style annotator regression.

Synthetic events mirroring the Angular sample portal's interaction shape:
mat-select panel-open clicks with options_seen, mat-checkbox boolean
clicks, ng-multiselect-dropdown server-search pickers, cdk-virtual-scroll
result lists.

Asserts:

  B2.1: every CLICKED labeled widget (mat-select, mat-checkbox,
        ng-multiselect-dropdown root) becomes a SkillParam derived from
        the accessible_name (snake-cased, trailing ``*:`` stripped).

  B2.2: SelectOptionSpec.known_options is populated from options_seen
        unioned across the cluster's events.

  B2.3: the labeled-widget param's example reflects the LATEST
        current_value (the displayed value after the operator picked).

  B2.4: SetSelectionSpec.require_search is True when the cluster
        contains a search input, OR when the picker is
        ng-multiselect-dropdown, OR when the picker is a virtualized
        list with a sibling search bar.

  B2.5: ``_detect_search_box_for_list`` finds a sibling search input
        whose ancestor chain shares an id with the list.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pilot.annotate import (
    _assign_synthetic_ids,
    _declare_labeled_widget_params,
    _detect_search_box_for_list,
    _looks_like_search_input,
    _snake_from_label,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    OptionSnapshot,
    SkillParam,
    SkillStep,
    TraceEvent,
)


# ---- helpers ---------------------------------------------------------------


def _mat_select_click(
    event_id: str,
    *,
    elt_id: str,
    label: str,
    current_value: str | None,
    options_seen: list[tuple[str, str]] | None = None,
    sequence: int,
) -> TraceEvent:
    """A click on a mat-select widget root, as the batch-1 grabber would
    capture (semantic widget root, preceding-sibling label, current
    display value, optional options_seen on the panel-open)."""
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
        page_url="http://localhost:5189/transfer-artwork",
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
    """A click on a mat-option inside the open panel."""
    fp = ElementFingerprint(
        tag="mat-option",
        role="option",
        element_id=opt_id,
        text=opt_label,
        accessible_name=opt_label,
    )
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=fp,
        page_url="http://localhost:5189/transfer-artwork",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _mat_checkbox_click(
    event_id: str,
    *,
    elt_id: str,
    label: str,
    aria_checked: str,
    sequence: int,
) -> TraceEvent:
    fp = ElementFingerprint(
        tag="mat-checkbox",
        role="checkbox",
        element_id=elt_id,
        accessible_name=label,
        current_value=aria_checked,
    )
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=fp,
        page_url="http://localhost:5189/transfer-artwork",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_click",
    )


# ---- B2.0: snake_from_label -----------------------------------------------


def test_snake_from_label_strips_required_marker() -> None:
    assert _snake_from_label("Target Model*: ") == "target_model"
    assert _snake_from_label("Year*:") == "year"
    assert _snake_from_label("Country/Region*: ") == "country_region"
    assert _snake_from_label(" Include Metadata? ") == "include_metadata"
    assert _snake_from_label("") is None
    assert _snake_from_label(None) is None
    assert _snake_from_label("***") is None


# ---- B2.1: labeled widget -> param ----------------------------------------


def test_b21_mat_select_becomes_param() -> None:
    """A click on a labeled mat-select declares a param whose name is
    the snake-cased label and whose type is 'enum'."""
    events = _assign_synthetic_ids([
        _mat_select_click(
            "e1",
            elt_id="mat-select-year",
            label="Year*: ",
            current_value="2024",
            options_seen=[("2024", "2024"), ("2023", "2023"), ("2022", "2022")],
            sequence=1,
        ),
    ])
    skill = build_skill(skill_name="ng_test", events=events, auto=True)

    names = {p.name for p in skill.params}
    assert "year" in names
    year = next(p for p in skill.params if p.name == "year")
    assert year.type == "enum"
    # B2.2: known_options populated from options_seen.
    enum_labels = {o.label for o in (year.enum_options or [])}
    assert enum_labels == {"2024", "2023", "2022"}


def test_b21_mat_checkbox_becomes_boolean_param() -> None:
    """A labeled mat-checkbox click declares a boolean param."""
    events = _assign_synthetic_ids([
        _mat_checkbox_click(
            "e1",
            elt_id="mat-checkbox-meta",
            label="Include Metadata: ",
            aria_checked="true",
            sequence=1,
        ),
    ])
    skill = build_skill(skill_name="ng_test", events=events, auto=True)
    names = {p.name for p in skill.params}
    assert "include_metadata" in names
    meta = next(p for p in skill.params if p.name == "include_metadata")
    assert meta.type == "boolean"


def test_b21_unlabeled_click_does_not_create_param() -> None:
    """A click on a widget WITHOUT an accessible_name is NOT bound as a
    param -- prevents the noise of clicks on transient div surfaces."""
    fp = ElementFingerprint(tag="mat-select", role="listbox", current_value="x")
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=fp,
        page_url="http://x",
        event_id="e1",
        sequence=1,
        source="user_click",
    )
    events = _assign_synthetic_ids([ev])
    skill = build_skill(skill_name="t", events=events, auto=True)
    # No labeled-widget params declared.
    assert skill.params == [] or all(
        p.type not in ("enum", "boolean") for p in skill.params
    )


# ---- B2.3: latest current_value wins as example ---------------------------


def test_b23_latest_current_value_wins_as_example() -> None:
    """Two clicks on the SAME mat-select with DIFFERENT current_value
    (the operator picked a different option in between): the param's
    example reflects the LATEST current_value, not the first."""
    events = _assign_synthetic_ids([
        # First click -- the widget shows '24_BOMRB_8K' as current.
        _mat_select_click(
            "e1",
            elt_id="mat-select-model",
            label="Target Model*:",
            current_value="24_BOMRB_8K",
            options_seen=[
                ("o1", "24_BOMRB_8K"),
                ("o2", "24_KANTM2_8K"),
            ],
            sequence=1,
        ),
        # Operator picked 24_KANTM2_8K -- second click on the same widget
        # would show the new current_value.
        _mat_select_click(
            "e2",
            elt_id="mat-select-model",
            label="Target Model*:",
            current_value="24_KANTM2_8K",
            sequence=2,
        ),
    ])
    skill = build_skill(skill_name="ng_test", events=events, auto=True)
    target_model = next(p for p in skill.params if p.name == "target_model")
    # Latest current_value wins, not the first.
    assert target_model.example == "24_KANTM2_8K"


# ---- B2.4: require_search on set_selection --------------------------------


def test_b24_require_search_when_cluster_has_search_input() -> None:
    """When the multi-select cluster contains an input_change on the
    search field, require_search becomes True."""
    from pilot.annotate import _detect_set_selection_clusters, build_causality_graph

    events = _assign_synthetic_ids([
        TraceEvent(
            ts=datetime.utcnow(),
            kind="click",
            fingerprint=ElementFingerprint(
                test_id="multiselect-country-toggle",
                tag="button",
            ),
            page_url="http://x",
            event_id="c1",
            sequence=1,
            source="user_click",
        ),
        TraceEvent(
            ts=datetime.utcnow(),
            kind="input_change",
            fingerprint=ElementFingerprint(
                test_id="multiselect-country-search",
                tag="input",
                placeholder="Search",
            ),
            value="can",
            page_url="http://x",
            event_id="i1",
            sequence=2,
            source="user_input",
        ),
        TraceEvent(
            ts=datetime.utcnow(),
            kind="click",
            fingerprint=ElementFingerprint(
                test_id="multiselect-country-checkbox-ca",
                tag="input",
                role="checkbox",
                accessible_name="Canada",
            ),
            page_url="http://x",
            event_id="c2",
            sequence=3,
            source="user_click",
        ),
        TraceEvent(
            ts=datetime.utcnow(),
            kind="click",
            fingerprint=ElementFingerprint(
                test_id="multiselect-country-toggle",
                tag="button",
            ),
            page_url="http://x",
            event_id="c3",
            sequence=4,
            source="user_click",
        ),
    ])
    skill = build_skill(skill_name="ng_country", events=events, auto=True)
    spec = next(
        s.set_selection for s in skill.steps if s.action == "set_selection"
    )
    assert spec.require_search is True
    assert spec.search_fp is not None


def test_b24_require_search_when_picker_is_ng_multiselect() -> None:
    """When the picker's open_fp tag is ng-multiselect-dropdown,
    require_search is True even if the operator never touched the
    search input (server-side filter is mandatory on replay)."""
    events = _assign_synthetic_ids([
        TraceEvent(
            ts=datetime.utcnow(),
            kind="click",
            fingerprint=ElementFingerprint(
                test_id="multiselect-country-toggle",
                tag="ng-multiselect-dropdown",
                accessible_name="Country/Region*:",
            ),
            page_url="http://x",
            event_id="c1",
            sequence=1,
            source="user_click",
        ),
        TraceEvent(
            ts=datetime.utcnow(),
            kind="click",
            fingerprint=ElementFingerprint(
                test_id="multiselect-country-checkbox-ca",
                tag="input",
                role="checkbox",
                accessible_name="Canada",
            ),
            page_url="http://x",
            event_id="c2",
            sequence=2,
            source="user_click",
        ),
        TraceEvent(
            ts=datetime.utcnow(),
            kind="click",
            fingerprint=ElementFingerprint(
                test_id="multiselect-country-toggle",
                tag="ng-multiselect-dropdown",
            ),
            page_url="http://x",
            event_id="c3",
            sequence=3,
            source="user_click",
        ),
    ])
    skill = build_skill(skill_name="ng_country", events=events, auto=True)
    spec = next(
        s.set_selection for s in skill.steps if s.action == "set_selection"
    )
    assert spec.require_search is True


# ---- B2.5: _detect_search_box_for_list ------------------------------------


def test_b25_detect_search_box_via_shared_ancestor() -> None:
    """A virtualized list whose ancestor chain shares an id with a
    search input means the picker has an out-of-band search bar."""
    list_fp = ElementFingerprint(
        tag="cdk-virtual-scroll-viewport",
        test_id="left-viewport",
        ancestor_chain=[
            {"tag": "div", "id": "left-panel"},
            {"tag": "div", "className": "ccMapWLft"},
        ],
    )
    search_input_ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            tag="input",
            test_id="left-search",
            placeholder="Search",
            ancestor_chain=[
                {"tag": "div", "id": "left-panel"},
                {"tag": "div", "className": "ccMapWLft"},
            ],
        ),
        value="canada",
        page_url="http://x",
        event_id="i1",
        sequence=1,
        source="user_input",
    )
    found = _detect_search_box_for_list(list_fp, [search_input_ev])
    assert found is not None
    assert found.test_id == "left-search"


def test_b25_no_search_box_when_no_shared_ancestor() -> None:
    """An unrelated search elsewhere on the page is NOT linked to the
    list -- the picker would not have require_search set."""
    list_fp = ElementFingerprint(
        tag="cdk-virtual-scroll-viewport",
        test_id="left-viewport",
        ancestor_chain=[
            {"tag": "div", "id": "left-panel"},
        ],
    )
    unrelated_search = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            tag="input",
            test_id="header-search",
            placeholder="Search",
            ancestor_chain=[{"tag": "div", "id": "header"}],
        ),
        value="x",
        page_url="http://x",
        event_id="i1",
        sequence=1,
        source="user_input",
    )
    found = _detect_search_box_for_list(list_fp, [unrelated_search])
    assert found is None


def test_b25_looks_like_search_input_placeholder() -> None:
    fp = ElementFingerprint(tag="input", placeholder="Search")
    assert _looks_like_search_input(fp) is True


def test_b25_looks_like_search_input_aria_label() -> None:
    fp = ElementFingerprint(tag="input", aria_label="multiselect-search")
    assert _looks_like_search_input(fp) is True


def test_b25_looks_like_search_input_negative() -> None:
    fp = ElementFingerprint(tag="input", placeholder="Username")
    assert _looks_like_search_input(fp) is False


# ---- B2.2: known_options on select_option ---------------------------------


def test_b22_select_option_spec_unions_options_seen() -> None:
    """When a cluster's events carry options_seen, the SelectOptionSpec's
    known_options is populated as the union (first-label-wins)."""
    from pilot.annotate import (
        _build_select_option_spec,
        build_causality_graph,
    )
    from pilot.skill_models import SemanticCluster

    # Two events both carrying options_seen; the union spans both.
    e1 = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            tag="mat-select",
            element_id="mat-select-make",
            accessible_name="Target Make*:",
            current_value="Samsung",
        ),
        page_url="http://x",
        event_id="e1",
        sequence=1,
        source="user_click",
    )
    e1.options_seen = [
        OptionSnapshot(value="o1", label="Samsung"),
        OptionSnapshot(value="o2", label="LG"),
    ]
    e2 = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            tag="mat-select",
            element_id="mat-select-make",
            accessible_name="Target Make*:",
            current_value="LG",
        ),
        page_url="http://x",
        event_id="e2",
        sequence=2,
        source="user_click",
    )
    e2.options_seen = [
        OptionSnapshot(value="o2", label="LG"),
        OptionSnapshot(value="o3", label="Sony"),
    ]
    events = _assign_synthetic_ids([e1, e2])
    causality = build_causality_graph(events)
    cluster = SemanticCluster(
        raw_event_ids=[e1.event_id, e2.event_id],
        cluster_kind="select_option",
        primary_target_event_id=e2.event_id,
    )
    spec = _build_select_option_spec(cluster, events, causality, e2)
    ids = {o.value: o.label for o in spec.known_options}
    assert ids == {"o1": "Samsung", "o2": "LG", "o3": "Sony"}


# ---- B2.4: require_search on SelectOptionSpec via search input ------------


def test_b24_select_option_require_search_when_search_input_in_cluster() -> None:
    """A select_option cluster that includes a search input_change event
    has require_search=True on its spec."""
    from pilot.annotate import (
        _build_select_option_spec,
        build_causality_graph,
    )
    from pilot.skill_models import SemanticCluster

    e_open = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            tag="mat-select",
            element_id="mat-select-country",
            accessible_name="Country*:",
        ),
        page_url="http://x",
        event_id="e1",
        sequence=1,
        source="user_click",
    )
    e_search = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            tag="input",
            placeholder="Search",
            accessible_name="Search",
        ),
        value="can",
        page_url="http://x",
        event_id="e2",
        sequence=2,
        source="user_input",
    )
    e_pick = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            tag="mat-option",
            element_id="mat-option-ca",
            text="Canada",
        ),
        value="ca",
        page_url="http://x",
        event_id="e3",
        sequence=3,
        source="user_click",
    )
    e_pick.options_seen = [OptionSnapshot(value="ca", label="Canada")]
    events = _assign_synthetic_ids([e_open, e_search, e_pick])
    causality = build_causality_graph(events)
    cluster = SemanticCluster(
        raw_event_ids=[e_open.event_id, e_search.event_id, e_pick.event_id],
        cluster_kind="select_option",
        primary_target_event_id=e_pick.event_id,
    )
    spec = _build_select_option_spec(cluster, events, causality, e_pick)
    assert spec.require_search is True
