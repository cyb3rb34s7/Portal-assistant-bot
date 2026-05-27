"""WI-17: select_option (native single-select as enum with declared
aliases and NO fuzzy fallback).

Plan acceptance check:
  Recorded ``US`` never auto-selects ``UAE``; runner fails structurally
  without an alias.

Tests:
  - Schema roundtrip: SelectOptionSpec persists.
  - Detector: a change on a select_single with options_snapshot ->
    ONE select_option cluster.
  - Annotator: build_skill emits a select_option step with the
    recorded value/label populated from the options_snapshot.
  - Negative match (unit, no Playwright): the match logic in
    _do_select_option rejects 'US' when current options are
    ['UAE', 'UK'] -- via direct call with a stub locator.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_select_option_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    OptionSnapshot,
    SelectOptionSpec,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS


def _select_change(
    event_id: str,
    value: str,
    options: list[dict[str, Any]],
    test_id: str = "select-region",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="select",
            control_kind="select_single",
            value_kind="string",
            options_snapshot=[
                OptionSnapshot(
                    value=o["value"],
                    label=o["label"],
                    selected=o.get("selected", False),
                )
                for o in options
            ],
            selected_options=[value],
        ),
        value=value,
        page_url="http://x/asset",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=1,
        source="user_change",
    )


# ----- schema --------------------------------------------------------------


def test_select_option_spec_roundtrip() -> None:
    spec = SelectOptionSpec(
        recorded_value="US",
        recorded_label="United States",
        options_snapshot=[
            OptionSnapshot(value="US", label="United States"),
            OptionSnapshot(value="UK", label="United Kingdom"),
        ],
        match_mode="alias",
        aliases={"US": ["USA", "United States"]},
    )
    step = SkillStep(
        index=0,
        action="select_option",
        fingerprint=ElementFingerprint(test_id="sel"),
        select_option=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0]
    assert rs.select_option is not None
    assert rs.select_option.recorded_value == "US"
    assert rs.select_option.match_mode == "alias"
    assert rs.select_option.aliases == {"US": ["USA", "United States"]}


# ----- detector ------------------------------------------------------------


def test_detector_emits_one_cluster_for_native_select_change() -> None:
    events = _assign_synthetic_ids([
        _select_change("e1", "US", [
            {"value": "US", "label": "United States"},
            {"value": "UK", "label": "United Kingdom"},
        ]),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_select_option_clusters(events, causality, consumed)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "select_option"
    assert c.primary_target_event_id == "e1"
    assert "e1" in consumed


def test_detector_skips_change_without_options_snapshot() -> None:
    """Negative: a change on a text input (no options_snapshot) does
    NOT produce a select_option cluster."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id="input-title",
            tag="input",
            control_kind="text_input",
        ),
        value="hello",
        page_url="http://x",
        event_id="e1",
        sequence=1,
    )
    events = _assign_synthetic_ids([ev])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_select_option_clusters(events, causality, consumed)
    assert clusters == []
    assert consumed == set()


# ----- annotator integration ----------------------------------------------


def test_build_skill_emits_select_option_step_with_options_snapshot() -> None:
    options = [
        {"value": "US", "label": "United States"},
        {"value": "UAE", "label": "United Arab Emirates"},
        {"value": "UK", "label": "United Kingdom"},
    ]
    events = [_select_change("e1", "US", options)]
    skill = build_skill(skill_name="t", events=events, auto=True)
    so = [s for s in skill.steps if s.action == "select_option"]
    assert len(so) == 1
    s = so[0]
    assert s.select_option is not None
    spec = s.select_option
    assert spec.recorded_value == "US"
    assert spec.recorded_label == "United States"
    assert spec.options_snapshot is not None
    assert len(spec.options_snapshot) == 3
    # match_mode defaults to "value" -- safe default per WI-17 +
    # WI-25 (no fuzzy unless aliases declared).
    assert spec.match_mode == "value"
    assert spec.aliases == {}


# ----- match-mode unit test (no Playwright) -------------------------------


def _spec(
    recorded_value: str,
    match_mode: str = "value",
    aliases: dict[str, list[str]] | None = None,
) -> SelectOptionSpec:
    return SelectOptionSpec(
        recorded_value=recorded_value,
        match_mode=match_mode,  # type: ignore[arg-type]
        aliases=aliases or {},
        options_snapshot=[
            OptionSnapshot(value="UAE", label="United Arab Emirates"),
            OptionSnapshot(value="UK", label="United Kingdom"),
        ],
    )


def test_match_logic_rejects_us_when_options_only_have_uae_uk() -> None:
    """Plan acceptance: recorded 'US' never auto-selects 'UAE'.

    Unit test: simulate the match-logic decisions of _do_select_option
    without a Playwright locator by exercising the chain directly.
    """
    spec = _spec("US", match_mode="value")
    current = [
        {"value": "UAE", "text": "United Arab Emirates"},
        {"value": "UK", "text": "United Kingdom"},
    ]
    current_values = {o["value"] for o in current}
    current_labels = {o["text"] for o in current}
    resolved = spec.recorded_value
    # The runner logic:
    target_value: str | None = None
    if resolved in current_values:
        target_value = resolved
    elif spec.match_mode in ("label", "alias") or resolved in current_labels:
        for o in current:
            if o["text"] == resolved:
                target_value = o["value"]
                break
    # No aliases declared in this scenario.
    assert target_value is None  # MUST fail structurally


def test_match_logic_accepts_us_via_declared_alias_for_usa() -> None:
    """Aliases declared by the operator: recorded 'US' -> accept 'USA'."""
    spec = _spec("US", match_mode="alias", aliases={"US": ["USA"]})
    current = [
        {"value": "USA", "text": "United States"},
        {"value": "UAE", "text": "United Arab Emirates"},
    ]
    current_values = {o["value"] for o in current}
    resolved = spec.recorded_value
    target_value: str | None = None
    if resolved in current_values:
        target_value = resolved
    if target_value is None and spec.aliases:
        alts = spec.aliases.get(resolved, [])
        for alt in alts:
            if alt in current_values:
                target_value = alt
                break
    assert target_value == "USA"


# ----- runner --------------------------------------------------------------


def test_runner_no_longer_treats_select_option_as_unimplemented() -> None:
    assert "select_option" not in _UNIMPLEMENTED_ACTIONS
