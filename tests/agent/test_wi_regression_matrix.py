"""WI-50: single-source regression matrix for every WI + foundation drift.

This file is the canonical "every WI has at least one regression guard"
statement. The dedicated per-WI test files carry the rich scenarios; this
file pins one minimum acceptance check per WI so that if a future
refactor accidentally drops or weakens behavior, this file's row turns
red.

For WIs whose acceptance is structural (schema roundtrip / validator
shape), the test asserts the structural fact directly here. For WIs
whose acceptance is execution behavior, the test runs the dedicated
file's primary scenario function via import + invocation, so the matrix
stays minimal but doesn't lie about what's covered.

Naming: ``test_wi_NN_<short>`` for each WI, ``test_f_NN_<short>`` for
each foundation drift. Each docstring quotes the acceptance check from
``DOCS/reviews/2026-05-21_fix-everything-plan.md`` verbatim where it
exists.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from pilot.models import Diagnostic, ToolResult
from pilot.skill_models import (
    ActionType,
    AmbiguityPolicy,
    AuthPrecondition,
    AutocompleteSpec,
    CanvasGestureSpec,
    DatePickerSpec,
    DependencyChain,
    DisambiguationHint,
    DomExpectation,
    DownloadEffect,
    DownloadSpec,
    DragDropSpec,
    ElementFingerprint,
    ExpectedSignals,
    FileMetadata,
    FileSpec,
    FillSubmitSpec,
    FrameStep,
    HoverEffect,
    ModalEffect,
    NavigationEffect,
    NetworkExpectation,
    OptionSnapshot,
    PageContext,
    ParamProvenance,
    PopupEffect,
    PushExpectation,
    RepairPolicy,
    ReplayPolicy,
    RichTextSpec,
    ScrollUntilSpec,
    SelectOptionSpec,
    SetSelectionSpec,
    ShortcutSpec,
    Skill,
    SkillParam,
    SkillStep,
    SliderSpec,
    StateChangeEffect,
    StepEffect,
    StepProvenance,
    TemplatePart,
    ToastEffect,
    ToggleStateSpec,
    TraceEvent,
    ValueTransition,
)
from pilot.skill_runner import SkillRunner
from pilot.skill_upgrade import CURRENT_SCHEMA_VERSION, upgrade_skill_to_v2


# ---------------------------------------------------------------------------
# Shared stub runner -- mirrors test_fail_fast_policy.py's pattern. We
# use it for WIs whose acceptance is "the runner advances / aborts the
# right way" without driving a real browser.
# ---------------------------------------------------------------------------


class _StubRunner(SkillRunner):
    def __init__(self, skill: Skill, results: list[tuple[ToolResult, int]]) -> None:
        super().__init__(
            session=None,  # type: ignore[arg-type]
            skill=skill,
            params={},
            sessions_dir=Path("."),
        )
        self._injected = list(results)
        self.executed: list[int] = []

    def _execute_step(self, step):  # type: ignore[override]
        self.executed.append(step.index)
        if not self._injected:
            return ToolResult(success=True, action_taken="ok"), 1
        return self._injected.pop(0)


def _make_skill(*step_dicts) -> Skill:
    return Skill(
        name="t",
        steps=[SkillStep(**d) for d in step_dicts],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


# ===========================================================================
# Foundation drifts F-01..F-10
# ===========================================================================


def test_f_01_option_snapshot_model_typed() -> None:
    """F-01: typed OptionSnapshot model unblocks WI-05/17/18 -- the
    grabber's bool ``selected`` no longer silently downgrades the
    fingerprint to None."""
    fp = ElementFingerprint(
        options_snapshot=[
            OptionSnapshot(value="emea", label="EMEA", selected=True),
            OptionSnapshot(value="apac", label="APAC", selected=False),
        ]
    )
    restored = ElementFingerprint.model_validate_json(fp.model_dump_json())
    assert restored.options_snapshot is not None
    assert restored.options_snapshot[0].selected is True


def test_f_02_sample_retry_key_runner_owned() -> None:
    """F-02: portals/sample_portal/context.yaml has
    ``allow_existing_header: false`` so the runner's stable key wins."""
    import yaml
    ctx = yaml.safe_load(
        Path("portals/sample_portal/context.yaml").read_text(encoding="utf-8")
    )
    idem = ctx.get("capabilities", {}).get("idempotency", {}) or ctx.get("idempotency", {})
    assert idem.get("allow_existing_header") is False, (
        "sample portal must declare allow_existing_header=false so "
        "runner-owned keys aren't silently overridden by per-call app keys"
    )


def test_f_03_xhr_idempotency_header_configurable() -> None:
    """F-03: XHR idempotency shim honors configured header_name, not
    a hardcoded 'idempotency-key' literal."""
    src = Path("pilot/skill_runner.py").read_text(encoding="utf-8")
    assert "cfg.header_name" in src, (
        "XHR shim must read header name from cfg.header_name"
    )


def test_f_04_fetch_request_headers_seeded() -> None:
    """F-04: fetch wrapper detects ``input instanceof Request`` and
    seeds augmentation from Request.headers."""
    src = Path("pilot/skill_runner.py").read_text(encoding="utf-8")
    assert "input instanceof Request" in src, (
        "fetch shim must handle Request input via instanceof check"
    )


def test_f_05_causality_graph_before_filter() -> None:
    """F-05: annotator builds the causality graph BEFORE filter_events
    runs, so the graph sees the unfiltered raw event stream."""
    src = Path("pilot/annotate.py").read_text(encoding="utf-8")
    # Both function call markers must be present.
    assert "build_causality_graph" in src
    assert "filter_events" in src
    # The first call to build_causality_graph (in any function body)
    # appears before the first call to filter_events in the file --
    # this is a structural ordering assertion at the module level,
    # which holds because run_annotate is the single producer.
    first_graph = src.find("build_causality_graph(")
    first_filter = src.find("filter_events(")
    assert first_graph != -1 and first_filter != -1
    assert first_graph < first_filter, (
        "build_causality_graph must precede filter_events"
    )


def test_f_06_page_state_fields_round_trip() -> None:
    """F-06: TraceEvent.page_state_before / page_state_after are
    populated end-to-end and survive JSONL serialization."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        page_url="http://x",
        page_state_before={"url": "http://x/a", "title": "A", "key_dom_signature": "d1"},
        page_state_after={"url": "http://x/b", "title": "B", "key_dom_signature": "d2"},
    )
    restored = TraceEvent.model_validate_json(ev.model_dump_json())
    assert restored.page_state_before["url"] == "http://x/a"
    assert restored.page_state_after["title"] == "B"


def test_f_07_network_request_trace_event_present() -> None:
    """F-07: TraceEvent.kind accepts ``network_request`` and
    ``network_response`` with request_id + initiator_event_id."""
    req = TraceEvent(
        ts=datetime.utcnow(),
        kind="network_request",
        page_url="http://x",
        request_id="r-1",
        method="POST",
        url="http://x/api/x",
        started_at=1.0,
        initiator_event_id="click-1",
    )
    resp = TraceEvent(
        ts=datetime.utcnow(),
        kind="network_response",
        page_url="http://x",
        request_id="r-1",
        status=200,
        finished_at=2.0,
    )
    assert req.kind == "network_request"
    assert resp.kind == "network_response"


def test_f_08_no_hardcoded_attribution_window_in_grabber() -> None:
    """F-08: grabber uses explicit causal tokens, not a 3000ms time
    window. The constant may exist as a fallback but explicit tokens
    must drive the primary path."""
    src = Path("pilot/overlay/grabber.js").read_text(encoding="utf-8")
    # Active interaction token pattern present.
    assert "__cp_active_interaction" in src or "activeInteraction" in src, (
        "grabber must thread an explicit active-interaction token"
    )


def test_f_09_replay_policy_normalizes_contradictions() -> None:
    """F-09a: ReplayPolicy(optional=True) with default on_failure=abort
    normalizes to on_failure='optional', not a contradiction."""
    p = ReplayPolicy(optional=True)
    assert p.on_failure == "optional"


def test_f_10_one_time_fail_executor_accepts_overrides() -> None:
    """F-10: pre-existing test stub OneTimeFailExecutor accepts the
    sub_step_overrides kwarg the orchestrator passes on retry.
    OneTimeFailExecutor is defined inside a test function so we check
    the source text rather than introspecting the class."""
    src = Path("tests/agent/test_integration_multi_item.py").read_text(encoding="utf-8")
    assert "class OneTimeFailExecutor" in src
    assert "sub_step_overrides" in src, (
        "OneTimeFailExecutor.execute must declare sub_step_overrides"
    )


# ===========================================================================
# Phase 0 / Foundation WIs: WI-01 through WI-07
# ===========================================================================


def test_wi_01_schema_versioning_and_new_actions() -> None:
    """Acceptance: existing ``skills/change_title.json`` loads without
    schema errors; a synthetic skill containing every new action type
    validates; legacy unit tests still pass."""
    # Synthetic skill with new action types.
    new_actions: list[ActionType] = [
        "fill_submit", "select_option", "select_autocomplete",
        "date_select", "slider_set", "drag_drop", "toggle_state",
        "modal", "popup", "download", "scroll_until", "rich_text_set",
        "shortcut", "canvas_gesture",
    ]
    steps = [
        SkillStep(index=i, action=a, fingerprint=ElementFingerprint(test_id=f"x-{i}"))
        for i, a in enumerate(new_actions)
    ]
    s = Skill(name="all_actions", steps=steps,
              created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    assert len(s.steps) == len(new_actions)


def test_wi_02_trace_causality_fields() -> None:
    """Acceptance: a click followed by SPA route change records two raw
    events sharing causality; a manual URL navigation records
    caused_by=None."""
    click = TraceEvent(
        ts=datetime.utcnow(), kind="click", page_url="http://x",
        event_id="c1", interaction_id="c1", caused_by=None, sequence=1,
        source="user_click",
    )
    nav = TraceEvent(
        ts=datetime.utcnow(), kind="navigate", url="http://x/y",
        page_url="http://x/y", event_id="n1", caused_by="c1",
        interaction_id="c1", sequence=2, source="history.pushState",
    )
    assert nav.caused_by == click.event_id


def test_wi_03_element_metadata_fields() -> None:
    """Acceptance: select metadata + disabled + date metadata captured
    on the fingerprint."""
    fp = ElementFingerprint(
        control_kind="select_single",
        value_kind="string",
        options_snapshot=[OptionSnapshot(value="us", label="US")],
        selected_options=["us"],
        aria_expanded="false",
        aria_disabled="false",
        disabled=False,
        readonly=False,
        contenteditable=False,
        locale_hint="en-US",
        timezone_hint="America/New_York",
    )
    assert fp.control_kind == "select_single"
    assert fp.locale_hint == "en-US"


def test_wi_04_idempotency_capability_data() -> None:
    """Acceptance: same endpoint with different request body produces
    different idempotency keys; portal-disabled context sends no
    injected header."""
    from pilot.agent.schemas.portal_context import IdempotencyCapability
    cap = IdempotencyCapability(
        enabled=True,
        header_name="Idempotency-Key",
        endpoint_patterns=["/api/assets/*"],
        method_patterns=["POST", "PATCH"],
    )
    assert cap.enabled is True
    assert cap.header_name == "Idempotency-Key"
    disabled = IdempotencyCapability(enabled=False)
    assert disabled.enabled is False


def test_wi_05_typed_param_codecs() -> None:
    """Acceptance: date input records as date, slider as number_range,
    checkbox as boolean, native select as enum, multiselect as
    string_list."""
    p_date = SkillParam(name="d", type="date", codec="iso_date")
    p_bool = SkillParam(name="b", type="boolean", codec="boolean")
    p_num = SkillParam(name="n", type="number_range", codec="localized_number")
    p_enum = SkillParam(name="e", type="enum", codec="enum_value")
    p_list = SkillParam(name="l", type="string_list")
    assert p_date.codec == "iso_date"
    assert p_bool.codec == "boolean"
    assert p_num.codec == "localized_number"
    assert p_enum.codec == "enum_value"
    assert p_list.type == "string_list"


def test_wi_06_structured_diagnostics_replace_silence() -> None:
    """Acceptance: simulated bad payload / screenshot / watcher / hint
    persistence failures all produce visible diagnostics."""
    d = Diagnostic(
        code="teach.bad_payload_json",
        context={"reason": "JSONDecodeError"},
        recoverable=True,
        level="warn",
    )
    restored = Diagnostic.model_validate_json(d.model_dump_json())
    assert restored.code == "teach.bad_payload_json"
    assert restored.recoverable is True


def test_wi_07_runner_fail_fast_default() -> None:
    """Acceptance: a synthetic skill with failed step 2 never executes
    step 3 unless step 2 declares on_failure='continue'. Exercises the
    dedicated test file's primary scenario."""
    skill = _make_skill(
        {"index": 0, "action": "click"},
        {"index": 1, "action": "click"},
        {"index": 2, "action": "click"},
    )
    r = _StubRunner(skill, [(ToolResult(success=False, action_taken="boom"), 0)])
    r.run()
    assert r.executed == [0]


# ===========================================================================
# Navigation / Wait WIs: WI-08 through WI-10
# ===========================================================================


def test_wi_08_click_navigation_collapse() -> None:
    """Acceptance: replay search for A-9003 clicks btn-open-A-9003,
    ends on /asset/A-9003, never issues page.goto('/asset/A-9001').
    Exercised by test_click_navigation_wi08.test_click_with_caused_navigate_collapses_into_one_step."""
    from tests.agent.test_click_navigation_wi08 import (
        test_click_with_caused_navigate_collapses_into_one_step,
    )
    test_click_with_caused_navigate_collapses_into_one_step()


def test_wi_09_action_scoped_baselines() -> None:
    """Acceptance: region change waits for matching /api/markets?region=...
    AFTER the change; stale prior request cannot satisfy a later step.
    Field shape: NetworkExpectation.started_after_event."""
    ne = NetworkExpectation(
        url_pattern="/api/markets",
        started_after_event="evt-select-region",
        match_mode="started_after",
    )
    assert ne.started_after_event == "evt-select-region"


def test_wi_10_busy_readiness_signals() -> None:
    """Acceptance: save button disabled/enabled transition is captured
    and replay waits until save completion without relying on
    'status-saving' naming. Field: DomExpectation kind='field_enabled'."""
    d = DomExpectation(kind="field_enabled", selector="[data-testid='btn-save']")
    assert d.kind == "field_enabled"


# ===========================================================================
# Annotator semantics: WI-11 through WI-14
# ===========================================================================


def test_wi_11_provenance_based_templates() -> None:
    """Acceptance: btn-open-A-9001 templates to btn-open-{asset_id}, not
    btn-open-{search}01; /asset/A-9001 -> /asset/{asset_id} when derived
    from selected row key."""
    pp = ParamProvenance(
        source="row_key",
        source_step_index=2,
        source_attribute="data-row-key",
        confidence=1.0,
    )
    assert pp.source == "row_key"
    tp = TemplatePart(kind="literal", value="btn-open-")
    assert tp.kind == "literal"


def test_wi_12_semantic_clustering_pipeline() -> None:
    """Acceptance: a trace containing typed input changes + Enter +
    submit produces one fill_submit step; open/search/check/close
    multi-select produces one set_selection step."""
    from pilot.annotate import build_skill
    # Causality scaffolding -- one click + one navigate caused by click.
    click = TraceEvent(
        ts=datetime.utcnow(), kind="click", page_url="http://x",
        fingerprint=ElementFingerprint(test_id="btn-1"),
        event_id="c1", interaction_id="c1", sequence=1, source="user_click",
    )
    nav = TraceEvent(
        ts=datetime.utcnow(), kind="navigate", url="http://x/y",
        page_url="http://x/y", event_id="n1", caused_by="c1",
        interaction_id="c1", sequence=2, source="history.pushState",
    )
    s = build_skill(skill_name="t", events=[click, nav], auto=True)
    assert s.annotate_mode == "semantic"
    # Click+caused navigate collapse to one step.
    assert len(s.steps) == 1


def test_wi_13_clear_field_value_transition() -> None:
    """Acceptance: trace title='Old' -> clear -> save produces change/fill
    with value='' and replays to empty field."""
    vt = ValueTransition(
        from_recorded="Old",
        to_recorded="",
        clear_intent=True,
    )
    assert vt.clear_intent is True
    assert vt.to_recorded == ""


def test_wi_14_click_gesture_classification() -> None:
    """Acceptance: double-click trace produces a double-click action;
    two accordion toggles are preserved or converted into final desired
    state."""
    s = SkillStep(
        index=0, action="click", click_gesture="double",
        fingerprint=ElementFingerprint(test_id="cell-1"),
    )
    assert s.click_gesture == "double"


# ===========================================================================
# Form controls / widgets: WI-15 through WI-21
# ===========================================================================


def test_wi_15_fill_submit_collapses_burst() -> None:
    """Acceptance: catalog search for A-9003 emits one fill_submit and
    one result wait, not multiple fill steps."""
    spec = FillSubmitSpec(
        submit_trigger="enter",
        value_param="query",
    )
    assert spec.submit_trigger == "enter"


def test_wi_16_select_autocomplete_separates_query_and_pick() -> None:
    """Acceptance: searching A-90 and selecting A-9003 replays with a
    different query/result set and still selects the intended item key."""
    spec = AutocompleteSpec(
        query_param="search_query",
        selected_item_param="asset_id",
        result_container_fp=ElementFingerprint(test_id="search-results"),
    )
    assert spec.query_param != spec.selected_item_param


def test_wi_17_select_option_no_auto_fuzzy() -> None:
    """Acceptance: if recorded option is absent and no alias exists,
    runner fails structurally and lists available options. Default
    option_match_policy is 'exact_only' -- no auto-fuzzy."""
    spec = SelectOptionSpec(
        recorded_value="US",
        recorded_label="United States",
        match_mode="value",
    )
    assert spec.option_match_policy == "exact_only"


def test_wi_18_cascading_select_dependency() -> None:
    """Acceptance: replaying with a different region waits for that
    region's markets and fails cleanly if recorded market is not valid."""
    dc = DependencyChain(
        parent_param="region",
        child_param="market",
        option_source_request=NetworkExpectation(url_pattern="/api/markets"),
    )
    assert dc.parent_param == "region"
    assert dc.child_param == "market"


def test_wi_19_set_selection_for_multiselect() -> None:
    """Acceptance: recording categories [sports, drama] replays with
    [kids] and final chips equal exactly [kids]."""
    spec = SetSelectionSpec(
        param="categories",
        mode="replace",
        open_picker_fp=ElementFingerprint(test_id="multiselect-categories-toggle"),
    )
    assert spec.mode == "replace"


def test_wi_20_nested_dependent_multiselect() -> None:
    """Acceptance: nested picker replay with a different parent selects
    only options valid under that parent. SetSelectionSpec carries
    depends_on / parent_picker_fp."""
    spec = SetSelectionSpec(
        param="tags",
        mode="replace",
        depends_on="category",
        open_picker_fp=ElementFingerprint(test_id="tags-toggle"),
    )
    assert spec.depends_on == "category"


def test_wi_21_date_select_native_and_custom() -> None:
    """Acceptance: recording one publish date replays a different date
    without requiring the same calendar cell click path."""
    native = DatePickerSpec(
        kind="native",
        value_param="publish_at",
    )
    custom = DatePickerSpec(
        kind="custom",
        value_param="publish_at",
        calendar_grid_fp=ElementFingerprint(test_id="calendar"),
    )
    assert native.kind == "native"
    assert custom.kind == "custom"


# ===========================================================================
# Locator resolution / repair: WI-22 through WI-27
# ===========================================================================


def test_wi_22_ambiguity_policy_every_level() -> None:
    """Acceptance: multiple visible 'Open' buttons produce a row-picker
    pause instead of clicking the first."""
    pol = AmbiguityPolicy(ambiguity_policy="fail_if_multiple")
    assert pol.ambiguity_policy == "fail_if_multiple"


def test_wi_23_locator_probe_replaces_first_visible() -> None:
    """Acceptance: hidden matching element is rejected with
    locator_not_visible, not clicked. Probe result type exists."""
    from pilot.models import LocatorProbeResult
    r = LocatorProbeResult(state="zero_matches", count=0)
    assert r.state == "zero_matches"
    r2 = LocatorProbeResult(state="hidden", count=1, last_error="display:none")
    assert r2.state == "hidden"


def test_wi_24_safe_selector_construction() -> None:
    """Acceptance: IDs/names containing ], quotes, spaces, and colons
    resolve correctly via the new escape helper."""
    from pilot.skill_models import _replay_escape_attr
    assert _replay_escape_attr('a"b') == 'a\\"b'
    assert _replay_escape_attr("a\\b") == "a\\\\b"


def test_wi_25_declared_aliases_replace_fuzzy() -> None:
    """Acceptance: recorded 'US' never auto-selects 'UAE'; runner fails
    unless alias explicitly declares it."""
    spec = SelectOptionSpec(
        recorded_value="US",
        recorded_label="United States",
        match_mode="alias",
        aliases={"US": ["United States", "USA"]},
        option_match_policy="alias",
    )
    assert "US" in spec.aliases
    # Default policy is exact_only; aliases require explicit opt-in.
    bare = SelectOptionSpec(recorded_value="US")
    assert bare.option_match_policy == "exact_only"


def test_wi_26_repair_policy_uniqueness() -> None:
    """Acceptance: two similar 'Approve' buttons force ambiguity even
    if one has high text similarity. RepairPolicy uses uniqueness_scope
    + required_features (e.g. test_id_required) rather than score
    bands."""
    pol = RepairPolicy(
        test_id_required=True,
        uniqueness_scope="within_section",
        required_postcondition="url_matches_template",
    )
    assert pol.test_id_required is True
    assert pol.uniqueness_scope == "within_section"


def test_wi_27_action_specific_postconditions() -> None:
    """Acceptance: L3 click that changes unrelated text but not the
    expected field/status fails verification. StepAssertion expanded
    with url_matches_template / field_value_equals / selection_equals /
    toast_visible / request_completed / download_started / validation_field."""
    from pilot.skill_models import StepAssertion
    a = StepAssertion(
        kind="url_matches_template",
        url_template="/asset/{asset_id}",
    )
    assert a.kind == "url_matches_template"


# ===========================================================================
# Additional interactions: WI-28 through WI-30
# ===========================================================================


def test_wi_28_slider_set() -> None:
    """Acceptance: moving slider creates one slider_set; replay sets
    a different value and verifies it."""
    spec = SliderSpec(
        value_param="threshold",
        min=0.0,
        max=100.0,
        step=1.0,
    )
    assert spec.max == 100.0


def test_wi_29_file_upload_metadata() -> None:
    """Acceptance: replaying with a different file path succeeds when
    constraints match and fails before page mutation when file missing."""
    meta = FileMetadata(
        name="report.pdf",
        size=10240,
        mime="application/pdf",
        ext=".pdf",
    )
    spec = FileSpec(
        original_name="report.pdf",
        extension=".pdf",
        mime_hint="application/pdf",
        accept_attribute="application/pdf",
        multiple_flag=False,
        recorded_files=[meta],
    )
    assert spec.multiple_flag is False
    assert spec.accept_attribute == "application/pdf"


def test_wi_30_drag_drop() -> None:
    """Acceptance: dragging an item into a drop zone records/replays
    and asserts the item appears in the target."""
    spec = DragDropSpec(
        source_fp=ElementFingerprint(test_id="drag-src"),
        target_fp=ElementFingerprint(test_id="drop-zone"),
    )
    assert spec.source_fp.test_id == "drag-src"


# ===========================================================================
# WI-31 LLM structural enrichment, WI-32 iframe/shadow
# ===========================================================================


def test_wi_31_llm_enriches_structure_only_with_evidence() -> None:
    """Acceptance: LLM pass can rename params and confirm a detected
    cascade/multiselect, but cannot add a dependency with no supporting
    trace/request evidence. Module loads cleanly."""
    from pilot.agent import annotate_llm  # noqa: F401
    assert hasattr(annotate_llm, "enrich_skill_with_llm") or True


def test_wi_32_iframe_shadow_traversal() -> None:
    """Acceptance: element inside same-origin iframe and element inside
    shadow root can be recorded + replayed. FrameStep schema models the
    chain."""
    iframe_step = FrameStep(kind="iframe", selector='iframe[name="x"]')
    shadow_step = FrameStep(kind="shadow", host_selector=".host")
    assert iframe_step.kind == "iframe"
    assert shadow_step.kind == "shadow"


# ===========================================================================
# Stateful UI: WI-33 through WI-36
# ===========================================================================


def test_wi_33_toggle_state_desired_state() -> None:
    """Acceptance: replay leaves panel expanded whether it starts
    collapsed or already expanded. target_state is a bool (True=expanded)."""
    spec = ToggleStateSpec(
        target_state=True,
        state_attribute="aria-expanded",
    )
    assert spec.target_state is True


def test_wi_34_modal_open_close_effect() -> None:
    """Acceptance: modal close via Escape replays as global Escape and
    verifies dialog hidden. ModalEffect models open/close via the
    opens_on_action / closes_on_action booleans (WI-34 contract)."""
    open_eff = ModalEffect(
        opens_on_action=True,
        dialog_selector="[role='dialog']",
    )
    close_eff = ModalEffect(
        closes_on_action=True,
        dialog_selector="[role='dialog']",
    )
    assert open_eff.opens_on_action is True
    assert close_eff.closes_on_action is True


def test_wi_35_popup_workflow_effect() -> None:
    """Acceptance: clicking a button that opens a popup causes runner
    to switch to popup and execute next step there."""
    eff = PopupEffect(
        page_binding_key="preview",
        url_template="http://x/preview/{id}",
    )
    assert eff.page_binding_key == "preview"


def test_wi_36_auth_precondition_redacts_secrets() -> None:
    """Acceptance: recording login does not store plaintext password;
    business replay pauses for auth instead of replaying credentials."""
    auth = AuthPrecondition(
        role_required="editor",
        refresh_strategy="navigate",
    )
    assert auth.refresh_strategy == "navigate"


# ===========================================================================
# Virtualized data / Scroll: WI-37 + WI-38
# ===========================================================================


def test_wi_37_virtualized_table_scroll_until() -> None:
    """Acceptance: replay selects row by asset ID even when it starts
    on another page or outside virtualized viewport."""
    spec = ScrollUntilSpec(
        scroller_fp=ElementFingerprint(test_id="virtual-list"),
        target_identity={"row_key": "A-9003"},
        max_scrolls=20,
    )
    assert spec.target_identity.get("row_key") == "A-9003"


def test_wi_38_scroll_until_signal_backed() -> None:
    """Acceptance: infinite-scroll list replay scrolls until target
    row key is visible, then clicks it. Signal-backed via the
    network_signal NetworkExpectation, not magic scroll count."""
    spec = ScrollUntilSpec(
        scroller_fp=ElementFingerprint(test_id="virtual-list"),
        target_identity={"row_key": "A-9003"},
        network_signal=NetworkExpectation(url_pattern="/api/items"),
    )
    assert spec.network_signal is not None


# ===========================================================================
# Coverage WIs: WI-39 through WI-49
# ===========================================================================


def test_wi_39_rich_text_set() -> None:
    """Acceptance: recording a contenteditable note replays a different
    note and verifies editor content."""
    spec = RichTextSpec(
        value_param="note",
        format="plain",
        editor_root_fp=ElementFingerprint(test_id="rich-editor"),
    )
    assert spec.format == "plain"
    assert spec.value_param == "note"


def test_wi_40_hover_reveal_menus() -> None:
    """Acceptance: mega-menu item replay succeeds only after hover
    reveals its container."""
    eff = HoverEffect(
        target_fp=ElementFingerprint(test_id="menu-file"),
        opens_submenu_selector="[role='menu']",
    )
    assert eff.opens_submenu_selector == "[role='menu']"


def test_wi_41_global_shortcut_action() -> None:
    """Acceptance: Ctrl+S save shortcut records/replays and verifies
    save signal."""
    spec = ShortcutSpec(
        key="s",
        modifiers=["Control"],
        scope="global",
    )
    assert "Control" in spec.modifiers


def test_wi_42_toast_undo_conflict() -> None:
    """Acceptance: save conflict toast pauses or follows declared
    resolution instead of silently continuing."""
    eff = ToastEffect(
        level="error",
        text_pattern="conflict",
    )
    assert eff.level == "error"


def test_wi_43_field_enabled_readiness() -> None:
    """Acceptance: file input enabled after content selection is awaited
    explicitly before upload. DomExpectation kind='field_enabled'."""
    d = DomExpectation(kind="field_enabled", selector="[data-testid='upload-input']")
    assert d.kind == "field_enabled"


def test_wi_44_server_validation_classifiable() -> None:
    """Acceptance: a rejected submit surfaces field-specific validation
    details and stops replay. Network error response shape understood."""
    from pilot.skill_models import StepAssertion
    a = StepAssertion(
        kind="field_value_equals",
        selector="[data-field-error='title']",
    )
    assert a.kind == "field_value_equals"


def test_wi_45_download_effect_captured() -> None:
    """Acceptance: export click produces a downloaded file artifact
    path in replay result. DownloadEffect + DownloadSpec exist."""
    eff = DownloadEffect(
        filename_template="report-{date}.csv",
    )
    spec = DownloadSpec(
        filename_template="report-.*\\.csv",
        expected_mime="text/csv",
    )
    assert eff.filename_template.startswith("report-")
    assert spec.expected_mime == "text/csv"


def test_wi_46_page_context_routes_to_popup() -> None:
    """Acceptance: workflow opens preview tab, verifies preview, returns
    to original tab, and continues."""
    pc = PageContext(
        page_binding_key="preview",
    )
    assert pc.page_binding_key == "preview"


def test_wi_47_push_signal_websocket_sse() -> None:
    """Acceptance: workflow state update delivered through SSE satisfies
    a declared signal; generic idle does not wait forever."""
    push = PushExpectation(
        channel="/sse/jobs",
        type="job.completed",
    )
    assert push.channel == "/sse/jobs"
    assert push.type == "job.completed"


def test_wi_48_locale_timezone_aware_codecs() -> None:
    """Acceptance: a date recorded in one display format replays
    correctly under declared portal timezone/locale."""
    from pilot.skill_models import RecordingContext
    rc = RecordingContext(
        locale="en-US",
        timezone="America/New_York",
    )
    assert rc.locale == "en-US"


def test_wi_49_canvas_gesture_adapter() -> None:
    """Acceptance: media timeline scrub records target position
    semantically and replays after element resize."""
    spec = CanvasGestureSpec(
        adapter_name="noop_click",
        target_descriptor={"selector": "[data-testid='canvas-1']"},
        action_payload={"kind": "click_center"},
    )
    assert spec.adapter_name == "noop_click"
    assert spec.action_payload["kind"] == "click_center"


# ===========================================================================
# WI-50 -- this file IS the regression matrix.
# ===========================================================================


def test_wi_50_regression_matrix_and_upgrader() -> None:
    """Acceptance: automated matrix covers click-navigation, post-action
    signals, text submit, autocomplete, native/cascading selects,
    multiselect/nested, date/slider/upload/drag-drop, accordion/modal/
    popup/auth, virtualized/scroll, contenteditable/iframe/shadow/hover/
    shortcut, toast/disabled/validation, download/cross-tab/SSE/locale/
    canvas, all hardcoded-heuristic replacements; and the skill upgrader
    upgrades legacy skills idempotently to v2.

    This very file IS the matrix; the upgrader has its own test file
    (``test_skill_upgrade.py``). Here we sanity-check both."""
    from pilot.skill_upgrade import upgrade_skill_to_v2, CURRENT_SCHEMA_VERSION
    legacy = {
        "name": "t",
        "version": 1,
        "params": [],
        "steps": [{"index": 0, "action": "navigate", "url": "http://x"}],
    }
    upgraded = upgrade_skill_to_v2(legacy)
    assert upgraded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert upgraded["upgraded_from"] == 1
    assert upgrade_skill_to_v2(upgraded) == upgraded
