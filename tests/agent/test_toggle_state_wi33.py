"""WI-33: accordion / collapse as DESIRED state.

Plan acceptance check:
  Replay leaves the panel expanded regardless of starting state.
    - Already collapsed at replay -> click to expand.
    - Already expanded at replay -> NO-OP (no extra click).

Tests cover:
  - Schema roundtrip: ToggleStateSpec persists.
  - Detector: click on aria-expanded element with target_state_after
    -> ONE toggle_state cluster.
  - Detector: click on a non-toggle element is NOT clustered as
    toggle_state.
  - Annotator: build_skill emits a toggle_state step with the
    target_state derived from target_state_after.
  - Runner: when current aria-expanded == target_state -> no click.
  - Runner: when current aria-expanded != target_state -> one click,
    then verify.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_toggle_state_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    Skill,
    SkillStep,
    ToggleStateSpec,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS, SkillRunner


def _toggle_click(
    event_id: str,
    sequence: int,
    aria_expanded_before: bool,
    aria_expanded_after: bool,
    test_id: str = "accordion-advanced-toggle",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="button",
            role="button",
            aria_expanded=aria_expanded_before,
        ),
        target_state_before={"aria_expanded": aria_expanded_before},
        target_state_after={"aria_expanded": aria_expanded_after},
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_click",
    )


# ----- schema --------------------------------------------------------------


def test_toggle_state_spec_roundtrip() -> None:
    spec = ToggleStateSpec(
        target_state=True,
        state_attribute="aria-expanded",
        controlled_panel_selector="#advanced-panel",
    )
    step = SkillStep(
        index=0, action="toggle_state",
        fingerprint=ElementFingerprint(test_id="accordion-toggle"),
        toggle_state=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0].toggle_state
    assert rs is not None
    assert rs.target_state is True
    assert rs.state_attribute == "aria-expanded"
    assert rs.controlled_panel_selector == "#advanced-panel"


# ----- detector ------------------------------------------------------------


def test_detector_clusters_aria_expanded_click() -> None:
    """Click on element with aria-expanded -> ONE toggle_state cluster."""
    events = _assign_synthetic_ids([
        _toggle_click("e1", 1, aria_expanded_before=False, aria_expanded_after=True),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_toggle_state_clusters(events, causality, consumed)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "toggle_state"
    assert clusters[0].primary_target_event_id == "e1"
    assert "e1" in consumed


def test_detector_skips_non_toggle_click() -> None:
    """Click on an element WITHOUT aria-expanded is not a toggle."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(test_id="btn-save", role="button"),
        page_url="http://x",
        event_id="e1",
        sequence=1,
        source="user_click",
    )
    events = _assign_synthetic_ids([ev])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_toggle_state_clusters(events, causality, consumed)
    assert clusters == []


def test_detector_requires_target_state_after() -> None:
    """A click on aria-expanded element WITHOUT a captured
    target_state_after is NOT clustered (we can't derive the
    desired state)."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id="x", aria_expanded=False,
        ),
        target_state_before={"aria_expanded": False},
        target_state_after=None,  # not captured (legacy trace)
        page_url="http://x",
        event_id="e1",
        sequence=1,
        source="user_click",
    )
    events = _assign_synthetic_ids([ev])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_toggle_state_clusters(events, causality, consumed)
    assert clusters == []


# ----- annotator integration ----------------------------------------------


def test_build_skill_emits_toggle_state_step_with_target_from_after() -> None:
    """Toggle click false->true emits a toggle_state step with
    target_state=True (the post-click desired state)."""
    events = [
        _toggle_click("e1", 1, aria_expanded_before=False, aria_expanded_after=True),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    ts_steps = [s for s in skill.steps if s.action == "toggle_state"]
    assert len(ts_steps) == 1
    s = ts_steps[0]
    assert s.toggle_state is not None
    assert s.toggle_state.target_state is True
    assert s.toggle_state.state_attribute == "aria-expanded"
    # No stray click step for the same event.
    assert all(stp.action != "click" or stp.index != s.index for stp in skill.steps)


# ----- runner --------------------------------------------------------------


def test_runner_no_longer_treats_toggle_state_as_unimplemented() -> None:
    assert "toggle_state" not in _UNIMPLEMENTED_ACTIONS


def test_runner_noops_when_current_state_matches_target() -> None:
    """When the panel is ALREADY expanded and target is expanded,
    the runner returns success WITHOUT clicking."""
    locator = MagicMock()
    locator.get_attribute.return_value = "true"  # current aria-expanded
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    runner.session.page = MagicMock()
    runner._resolve_locator = MagicMock(return_value=(locator, 1, None))
    runner._screenshot = MagicMock(return_value=None)

    step = SkillStep(
        index=0,
        action="toggle_state",
        fingerprint=ElementFingerprint(test_id="accordion-toggle"),
        toggle_state=ToggleStateSpec(
            target_state=True, state_attribute="aria-expanded"
        ),
    )
    result, level = runner._do_toggle_state(step)
    assert result.success is True
    assert "noop" in result.action_taken.lower()
    locator.click.assert_not_called()


def test_runner_clicks_when_current_state_does_not_match() -> None:
    """When current=False and target=True, the runner clicks ONCE,
    then verifies the new state matches."""
    locator = MagicMock()
    # First read: current=false; after click: aria-expanded=true.
    locator.get_attribute.side_effect = ["false", "true"]
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    runner.session.page = MagicMock()
    runner._resolve_locator = MagicMock(return_value=(locator, 1, None))
    runner._screenshot = MagicMock(return_value=None)

    step = SkillStep(
        index=0,
        action="toggle_state",
        fingerprint=ElementFingerprint(test_id="accordion-toggle"),
        toggle_state=ToggleStateSpec(
            target_state=True, state_attribute="aria-expanded"
        ),
    )
    result, level = runner._do_toggle_state(step)
    assert result.success is True
    locator.click.assert_called_once()


def test_runner_fails_when_post_click_state_does_not_match() -> None:
    """If after the click the attribute still doesn't match target,
    the result is a typed failure (toggle_state_mismatch)."""
    locator = MagicMock()
    # current=false, then post-click still false (the click didn't
    # toggle the state -- portal bug or stale handler).
    locator.get_attribute.side_effect = ["false", "false"]
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    runner.session.page = MagicMock()
    runner._resolve_locator = MagicMock(return_value=(locator, 1, None))
    runner._screenshot = MagicMock(return_value=None)

    step = SkillStep(
        index=0,
        action="toggle_state",
        fingerprint=ElementFingerprint(test_id="x"),
        toggle_state=ToggleStateSpec(
            target_state=True, state_attribute="aria-expanded"
        ),
    )
    result, level = runner._do_toggle_state(step)
    assert result.success is False
    assert result.error_kind == "toggle_state_mismatch"
