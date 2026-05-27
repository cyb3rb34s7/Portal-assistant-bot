"""WI-40: hover-driven menus / mega menus.

Plan acceptance check:
  A 'File > Save As' mega-menu click records + replays even though
  the submenu doesn't render until the parent is hovered. The grabber
  emits a kind='hover' event when a pointerenter on a menu trigger
  caused a submenu to appear within the window; the annotator pairs
  the hover with the next click and folds it into effects.hover; the
  runner moves the mouse to the trigger, waits for the declared
  submenu selector, then clicks the child.

Tests cover the deterministic pipeline (no live browser):
  - Schema: HoverEffect roundtrip + StepEffect.hover.
  - Schema: TraceEvent.kind accepts 'hover'; submenu_selector +
    dwell_ms are optional.
  - Annotator: hover -> click pair folds into the click step's
    effects.hover; the hover does NOT produce its own step.
  - Annotator: hover followed by a non-click action drops the hover
    (observational only).
  - Annotator: unpaired hover (no following click) is silently
    skipped.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import build_skill
from pilot.skill_models import (
    ElementFingerprint,
    HoverEffect,
    SkillStep,
    StepEffect,
    TraceEvent,
)


def _hover_event(
    event_id: str,
    sequence: int,
    *,
    test_id: str = "nav-file-menu",
    submenu: str | None = "[data-testid='nav-file-submenu']",
    dwell_ms: int = 180,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="hover",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="li",
            role="menuitem",
            text="File ▾",
        ),
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_hover",
        raw_event_kind="pointerenter",
        submenu_selector=submenu,
        dwell_ms=dwell_ms,
    )


def _click_event(
    event_id: str,
    sequence: int,
    *,
    test_id: str = "menu-file-save-as",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="button",
            role="menuitem",
            text="Save As...",
            control_kind="button",
        ),
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_click",
        raw_event_kind="click",
    )


# ----- schema --------------------------------------------------------------


def test_hover_effect_roundtrip() -> None:
    he = HoverEffect(
        target_fp=ElementFingerprint(test_id="nav-file-menu"),
        dwell_ms=180,
        opens_submenu_selector="[data-testid='nav-file-submenu']",
    )
    payload = he.model_dump()
    restored = HoverEffect(**payload)
    assert restored.dwell_ms == 180
    assert restored.target_fp.test_id == "nav-file-menu"
    assert restored.opens_submenu_selector == "[data-testid='nav-file-submenu']"


def test_step_effect_carries_hover() -> None:
    eff = StepEffect(
        hover=HoverEffect(
            target_fp=ElementFingerprint(test_id="nav-file-menu"),
            dwell_ms=100,
            opens_submenu_selector="[role='menu']",
        )
    )
    payload = eff.model_dump()
    restored = StepEffect(**payload)
    assert restored.hover is not None
    assert restored.hover.opens_submenu_selector == "[role='menu']"


def test_trace_event_accepts_hover_kind() -> None:
    ev = _hover_event("h1", 1)
    assert ev.kind == "hover"
    assert ev.submenu_selector == "[data-testid='nav-file-submenu']"
    assert ev.dwell_ms == 180


# ----- annotator pairing ---------------------------------------------------


def test_hover_then_click_folds_into_click_effects() -> None:
    hover = _hover_event("h1", 1)
    click = _click_event("c1", 2)
    skill = build_skill(
        skill_name="mega_menu",
        events=[hover, click],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    # The hover should NOT produce its own step.
    actions = [s.action for s in skill.steps]
    # Exactly one click step (the menuitem).
    click_steps = [s for s in skill.steps if s.action == "click"]
    assert len(click_steps) == 1, (
        f"expected one click step, got {actions}"
    )
    cs = click_steps[0]
    # Effects.hover populated with the trigger fingerprint + selector.
    assert cs.effects is not None
    assert cs.effects.hover is not None
    assert cs.effects.hover.target_fp.test_id == "nav-file-menu"
    assert cs.effects.hover.opens_submenu_selector == (
        "[data-testid='nav-file-submenu']"
    )


def test_hover_followed_by_non_click_action_is_dropped() -> None:
    # Operator hovered then typed in a search field (not the menu
    # workflow); the hover must not attach to the typing.
    hover = _hover_event("h1", 1)
    typing = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id="input-search", control_kind="text_input"
        ),
        value="hello",
        page_url="http://x",
        event_id="i1",
        interaction_id="i1",
        sequence=2,
        source="user_input",
        raw_event_kind="input",
    )
    skill = build_skill(
        skill_name="hover_then_type",
        events=[hover, typing],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    # The input_change becomes a change step; no step carries hover.
    for s in skill.steps:
        if s.effects is not None:
            assert s.effects.hover is None
    assert any(s.action in ("change", "fill_submit") for s in skill.steps)


def test_unpaired_hover_drops_silently() -> None:
    # Hover at end of trace with no follow-up. Annotator skips it.
    hover = _hover_event("h1", 1)
    skill = build_skill(
        skill_name="hover_alone",
        events=[hover],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    assert skill.steps == []


def test_hover_event_does_not_appear_as_step() -> None:
    hover = _hover_event("h1", 1)
    click = _click_event("c1", 2)
    skill = build_skill(
        skill_name="paired",
        events=[hover, click],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    # No step has action='hover' (it isn't an ActionType anyway, but
    # the annotator must never try to emit one).
    for s in skill.steps:
        assert s.action != "hover"
