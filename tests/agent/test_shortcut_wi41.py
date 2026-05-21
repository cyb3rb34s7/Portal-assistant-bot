"""WI-41: global keyboard shortcuts.

Plan acceptance check:
  Ctrl+S as a save shortcut records once + replays without typing
  into the focused field. The grabber emits ``kind='key'`` with
  raw_event_kind='shortcut' for modifier+key chords; the annotator
  emits ONE ``shortcut`` step parameterized by modifiers + key; the
  runner uses ``page.keyboard.press(combo)``.

Tests cover the deterministic pipeline (no live browser):
  - Schema: ShortcutSpec roundtrip; SkillStep.shortcut.
  - Schema: TraceEvent.shortcut_modifiers optional.
  - Detector: a key event with raw_event_kind='shortcut' becomes a
    ``shortcut`` cluster, NOT a generic ``key`` step.
  - Annotator: build_skill emits one shortcut step with the modifiers
    + key on the spec.
  - Annotator: distinguishes shortcut from typing-key (an Enter key
    inside an input remains a ``key`` step, fed to fill_submit).
  - Runner: ``shortcut`` no longer in _UNIMPLEMENTED_ACTIONS.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _build_shortcut_spec,
    _detect_shortcut_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    ShortcutSpec,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS


def _shortcut_event(
    event_id: str,
    sequence: int,
    *,
    key: str = "s",
    modifiers: list[str] | None = None,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="key",
        fingerprint=ElementFingerprint(
            test_id="asset-page",
            tag="body",
        ),
        value=key,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_shortcut",
        raw_event_kind="shortcut",
        shortcut_modifiers=modifiers or ["Control"],  # type: ignore[arg-type]
    )


# ----- schema --------------------------------------------------------------


def test_shortcut_spec_roundtrip() -> None:
    spec = ShortcutSpec(
        modifiers=["Control"],
        key="s",
        scope="global",
        expected_effect="save_completes",
    )
    payload = spec.model_dump()
    restored = ShortcutSpec(**payload)
    assert restored.key == "s"
    assert restored.modifiers == ["Control"]
    assert restored.scope == "global"
    assert restored.expected_effect == "save_completes"


def test_skill_step_carries_shortcut_spec() -> None:
    step = SkillStep(
        index=0,
        action="shortcut",
        shortcut=ShortcutSpec(modifiers=["Meta"], key="k", scope="command_palette"),
    )
    payload = step.model_dump()
    restored = SkillStep(**payload)
    assert restored.shortcut is not None
    assert restored.shortcut.scope == "command_palette"


def test_trace_event_shortcut_modifiers_optional() -> None:
    ev = TraceEvent(ts=datetime.utcnow(), kind="click", page_url="http://x")
    assert ev.shortcut_modifiers is None


# ----- detector ------------------------------------------------------------


def test_detector_claims_shortcut_key_event() -> None:
    ev = _shortcut_event("k1", 1, key="s", modifiers=["Control"])
    causality = build_causality_graph([ev])
    consumed: set[str] = set()
    clusters = _detect_shortcut_clusters([ev], causality, consumed)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "shortcut"
    assert "k1" in consumed


def test_detector_skips_plain_key_event() -> None:
    # raw_event_kind='keydown' (the legacy text-input Enter path)
    # must NOT be claimed by the shortcut detector.
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="key",
        fingerprint=ElementFingerprint(
            test_id="input-search", tag="input", control_kind="text_input"
        ),
        value="Enter",
        page_url="http://x",
        event_id="k2",
        interaction_id="k2",
        sequence=1,
        source="user_keydown",
        raw_event_kind="keydown",
    )
    causality = build_causality_graph([ev])
    consumed: set[str] = set()
    clusters = _detect_shortcut_clusters([ev], causality, consumed)
    assert clusters == []
    assert "k2" not in consumed


# ----- spec build ----------------------------------------------------------


def test_build_spec_picks_up_modifiers() -> None:
    ev = _shortcut_event("k1", 1, key="k", modifiers=["Meta"])
    spec = _build_shortcut_spec(ev)
    assert spec is not None
    assert spec.key == "k"
    assert spec.modifiers == ["Meta"]
    assert spec.scope == "global"


def test_build_spec_dedupes_and_filters_modifiers() -> None:
    # Defensive filtering inside _build_shortcut_spec: even when the
    # source list contains stray values (e.g. from a hand-edited
    # trace), only valid Playwright modifiers survive into the spec.
    # We construct via model_construct so pydantic doesn't reject
    # the test fixture before the filtering can be exercised.
    ev = TraceEvent.model_construct(
        ts=datetime.utcnow(),
        kind="key",
        fingerprint=ElementFingerprint(test_id="x", tag="body"),
        value="s",
        page_url="http://x",
        event_id="k1",
        interaction_id="k1",
        sequence=1,
        source="user_shortcut",
        raw_event_kind="shortcut",
        shortcut_modifiers=["Control", "Bogus"],  # type: ignore[list-item]
    )
    spec = _build_shortcut_spec(ev)
    assert spec is not None
    assert "Bogus" not in spec.modifiers
    assert "Control" in spec.modifiers


# ----- annotator end-to-end -----------------------------------------------


def test_annotator_emits_shortcut_step() -> None:
    ev = _shortcut_event("k1", 1, key="s", modifiers=["Control"])
    skill = build_skill(
        skill_name="ctrl_s_save",
        events=[ev],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    shortcut_steps = [s for s in skill.steps if s.action == "shortcut"]
    assert len(shortcut_steps) == 1, (
        f"expected one shortcut step, got {[s.action for s in skill.steps]}"
    )
    sc = shortcut_steps[0].shortcut
    assert sc is not None
    assert sc.key == "s"
    assert sc.modifiers == ["Control"]


def test_plain_enter_stays_key_not_shortcut() -> None:
    # A typing-burst Enter (raw_event_kind='keydown') feeds fill_submit
    # or the legacy ``key`` action -- it must NOT become a shortcut.
    typing = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id="input-search", control_kind="text_input", tag="input"
        ),
        value="A-9001",
        page_url="http://x",
        event_id="i1",
        interaction_id="i1",
        sequence=1,
        raw_event_kind="input",
    )
    enter = TraceEvent(
        ts=datetime.utcnow(),
        kind="key",
        fingerprint=ElementFingerprint(
            test_id="input-search", tag="input", control_kind="text_input"
        ),
        value="Enter",
        page_url="http://x",
        event_id="k1",
        interaction_id="k1",
        sequence=2,
        source="user_keydown",
        raw_event_kind="keydown",
    )
    skill = build_skill(
        skill_name="typing_with_enter",
        events=[typing, enter],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    actions = [s.action for s in skill.steps]
    assert "shortcut" not in actions, (
        f"plain Enter must not become a shortcut; got {actions}"
    )


# ----- runner stub --------------------------------------------------------


def test_runner_shortcut_no_longer_unimplemented() -> None:
    assert "shortcut" not in _UNIMPLEMENTED_ACTIONS
