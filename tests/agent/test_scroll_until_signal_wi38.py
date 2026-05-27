"""WI-38: scroll_until backed by declared signal, not magic counts.

Plan acceptance check (ties together with WI-37):
  - DomExpectation has an explicit ``target_visible_in_scroller`` kind
    so the wait contract is signal-driven, not count-driven.
  - The runner's _do_scroll_until terminates as soon as the target
    becomes visible -- if the target is already in view, NO scrolls
    fire (the contract is "scroll one viewport, re-probe, repeat
    until visible or max_scrolls").
  - The scroller's scroll behavior comes from the declared
    ``scroller_fp`` -- never a hardcoded window / body.

Tests cover:
  - Schema: DomExpectation accepts kind="target_visible_in_scroller".
  - Runner: when _target_visible_in_scroller returns True on first
    probe, _do_scroll_until returns success with zero scrolls done.
  - Runner: when scroller_fp doesn't resolve, _do_scroll_until fails
    with error_kind="scroller_not_found" (no fallback to window).
  - Runner: when target_identity is empty, _do_scroll_until refuses
    to scroll (no blind scrolling).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pilot.skill_models import (
    DomExpectation,
    ElementFingerprint,
    ScrollUntilSpec,
    SkillStep,
)
from pilot.skill_runner import SkillRunner


# ----- schema --------------------------------------------------------------


def test_dom_expectation_target_visible_in_scroller_kind_validates() -> None:
    """WI-38: the explicit kind is part of the DomExpectation literal
    enum so the annotator can emit it without hacks."""
    de = DomExpectation(
        kind="target_visible_in_scroller",
        selector='[data-testid="asset-scroller"]',
        text='[data-testid="row-A-9075"]',
    )
    restored = DomExpectation.model_validate(de.model_dump())
    assert restored.kind == "target_visible_in_scroller"
    assert restored.selector == '[data-testid="asset-scroller"]'
    assert restored.text == '[data-testid="row-A-9075"]'


# ----- runner: zero-scroll fast path ---------------------------------------


def _runner_with_session(page_mock) -> SkillRunner:
    """Construct a SkillRunner instance bypassing the normal init so
    we can isolate _do_scroll_until without touching audit / playwright
    setup."""
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    runner.session.page = page_mock
    runner.audit = MagicMock()
    runner._diagnostic = MagicMock()
    # _level1 / _level2 need patching to return a mock locator.
    return runner


def test_do_scroll_until_skips_when_target_already_visible() -> None:
    """WI-38 signal-driven: when the target is visible on initial
    probe, NO scrolls fire. This is the contract the plan calls out
    ('replay against a list that has row 75 at any current scroll
    position')."""
    page = MagicMock()
    runner = _runner_with_session(page)
    scroller_mock = MagicMock()
    # _resolve_locator path: level1 returns the scroller.
    runner._level1 = MagicMock(return_value=scroller_mock)  # type: ignore[method-assign]
    runner._level2 = MagicMock(return_value=None)  # type: ignore[method-assign]
    runner._screenshot = MagicMock(return_value=None)  # type: ignore[method-assign]
    # First visibility probe returns True -- target is already in view.
    runner._target_visible_in_scroller = MagicMock(  # type: ignore[method-assign]
        return_value=True,
    )

    step = SkillStep(
        index=0,
        action="scroll_until",
        scroll_until=ScrollUntilSpec(
            scroller_fp=ElementFingerprint(test_id="asset-scroller"),
            target_identity={"test_id": "row-A-9075"},
            max_scrolls=50,
        ),
    )
    result, level = runner._do_scroll_until(step)
    assert result.success is True
    assert "already visible" in result.action_taken
    # scroller.evaluate must NOT have been called -- no scrolling fired.
    assert not scroller_mock.evaluate.called


def test_do_scroll_until_fails_when_scroller_not_found() -> None:
    """When the scroller_fp doesn't resolve, the runner fails with a
    specific error_kind instead of falling back to window / body.
    Per the plan: 'NOT a hardcoded window or body.'"""
    page = MagicMock()
    runner = _runner_with_session(page)
    runner._level1 = MagicMock(return_value=None)  # type: ignore[method-assign]
    runner._level2 = MagicMock(return_value=None)  # type: ignore[method-assign]
    runner._screenshot = MagicMock(return_value=None)  # type: ignore[method-assign]

    step = SkillStep(
        index=0,
        action="scroll_until",
        scroll_until=ScrollUntilSpec(
            scroller_fp=ElementFingerprint(test_id="missing-scroller"),
            target_identity={"test_id": "row-A-9075"},
            max_scrolls=50,
        ),
    )
    result, _ = runner._do_scroll_until(step)
    assert result.success is False
    assert result.error_kind == "scroller_not_found"


def test_do_scroll_until_refuses_blind_scrolling() -> None:
    """When target_identity is empty (no identifying attribute), the
    runner refuses to scroll. Prevents 'scroll for N times' anti-
    pattern from sneaking back in via under-specified specs."""
    page = MagicMock()
    runner = _runner_with_session(page)
    scroller_mock = MagicMock()
    runner._level1 = MagicMock(return_value=scroller_mock)  # type: ignore[method-assign]
    runner._level2 = MagicMock(return_value=None)  # type: ignore[method-assign]
    runner._screenshot = MagicMock(return_value=None)  # type: ignore[method-assign]

    step = SkillStep(
        index=0,
        action="scroll_until",
        scroll_until=ScrollUntilSpec(
            scroller_fp=ElementFingerprint(test_id="asset-scroller"),
            target_identity={},  # NO target identity.
            max_scrolls=50,
        ),
    )
    result, _ = runner._do_scroll_until(step)
    assert result.success is False
    assert result.error_kind == "scroll_target_undeclared"
    # No evaluate / scroll calls fired.
    assert not scroller_mock.evaluate.called


def test_do_scroll_until_bad_step_when_spec_missing() -> None:
    """Defensive: a scroll_until action with no spec returns bad_step
    -- a guard against the annotator failing to populate the field."""
    page = MagicMock()
    runner = _runner_with_session(page)
    runner._screenshot = MagicMock(return_value=None)  # type: ignore[method-assign]
    step = SkillStep(
        index=0, action="scroll_until", scroll_until=None,
    )
    result, _ = runner._do_scroll_until(step)
    assert result.success is False
    assert result.error_kind == "bad_step"
