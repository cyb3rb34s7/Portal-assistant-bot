"""Tests for WI-23: LocatorProbeResult replaces _first_visible.

Today's ``_first_visible`` catches all exceptions and falls back to
``count() > 0`` -- masking real failures (hidden / strict-mode /
detached / timeout) behind a "looks fine" boolean.

WI-23 replaces it with a structured ``LocatorProbeResult`` model and a
``_probe_locator(loc)`` helper. The runner's L1 / L2 / alternate paths
branch on the structured state via ``_probe_and_branch`` so hidden /
detached / strict-mode / timeout each get a diagnostic instead of
silently converting into "the next level didn't match."

Acceptance check (from the plan):
  Hidden matching element is rejected with locator_not_visible, not
  clicked. (Verified here by stubbed Locator + assertion on probe
  state.)
"""

from __future__ import annotations

import pytest
from playwright.sync_api import TimeoutError as PWTimeoutError

from pilot.models import LocatorProbeResult
from pilot.skill_runner import _first_visible, _probe_locator


class _StubLoc:
    """Tiny stub mimicking the Locator API the probe touches."""

    def __init__(
        self,
        *,
        count: int = 0,
        visible: bool = False,
        count_raises: Exception | None = None,
        visibility_raises: Exception | None = None,
    ):
        self._count = count
        self._visible = visible
        self._count_raises = count_raises
        self._visibility_raises = visibility_raises
        self.first = self

    def count(self):
        if self._count_raises is not None:
            raise self._count_raises
        return self._count

    def is_visible(self, timeout: int = 500):
        if self._visibility_raises is not None:
            raise self._visibility_raises
        return self._visible


# ---------------------------------------------------------------------
# LocatorProbeResult schema
# ---------------------------------------------------------------------


def test_locator_probe_result_states_are_distinct() -> None:
    """Each state literal must be acceptable so the runner can branch
    on it. Sanity-check that the model accepts each."""
    for state in [
        "visible_unique",
        "zero_matches",
        "multiple_matches",
        "hidden",
        "detached",
        "strict_mode_error",
        "timeout",
    ]:
        r = LocatorProbeResult(state=state, count=1)  # type: ignore[arg-type]
        assert r.state == state


def test_locator_probe_result_count_and_error_carry_failure_detail() -> None:
    """count + last_error carry structured failure detail the runner
    emits as a diagnostic when the probe doesn't succeed."""
    r = LocatorProbeResult(
        state="hidden", count=2, last_error="display: none"
    )
    assert r.count == 2
    assert r.last_error == "display: none"


# ---------------------------------------------------------------------
# _probe_locator behavior
# ---------------------------------------------------------------------


def test_probe_zero_matches_returns_zero_matches() -> None:
    probe = _probe_locator(_StubLoc(count=0))
    assert probe.state == "zero_matches"
    assert probe.count == 0


def test_probe_single_visible_returns_visible_unique() -> None:
    probe = _probe_locator(_StubLoc(count=1, visible=True))
    assert probe.state == "visible_unique"
    assert probe.count == 1


def test_probe_multiple_visible_returns_multiple_matches() -> None:
    """Hands off to WI-22's ambiguity scanner via the structured state
    instead of silently clicking first."""
    probe = _probe_locator(_StubLoc(count=4, visible=True))
    assert probe.state == "multiple_matches"
    assert probe.count == 4


def test_probe_count_match_but_hidden_returns_hidden() -> None:
    """Audit's key bug: ``count() > 0`` made hidden elements look
    clickable. The structured probe must distinguish hidden from
    visible_unique."""
    probe = _probe_locator(_StubLoc(count=1, visible=False))
    assert probe.state == "hidden"
    assert probe.last_error is not None


def test_probe_count_timeout_returns_timeout() -> None:
    probe = _probe_locator(
        _StubLoc(count_raises=PWTimeoutError("count took too long"))
    )
    assert probe.state == "timeout"
    assert probe.last_error is not None and "timeout" in probe.last_error


def test_probe_strict_mode_error_returns_strict_mode_error() -> None:
    """Playwright strict-mode rejection is preserved as its own state
    so the operator can see the resolver ambiguity, not just 'didn't
    match.'"""
    probe = _probe_locator(
        _StubLoc(count_raises=Exception("strict mode violation: resolved to 3"))
    )
    assert probe.state == "strict_mode_error"


def test_probe_detached_returns_detached() -> None:
    probe = _probe_locator(
        _StubLoc(
            count=1,
            visibility_raises=Exception(
                "element handle is detached from the DOM"
            ),
        )
    )
    assert probe.state == "detached"


def test_probe_visibility_timeout_returns_timeout() -> None:
    probe = _probe_locator(
        _StubLoc(
            count=1,
            visibility_raises=PWTimeoutError("visibility check timed out"),
        )
    )
    assert probe.state == "timeout"


# ---------------------------------------------------------------------
# Legacy _first_visible compatibility wrapper
# ---------------------------------------------------------------------


def test_legacy_first_visible_true_only_on_visible_states() -> None:
    """The wrapped legacy helper now returns True only when the probe
    says visible_unique or multiple_matches; every failure state
    (including hidden -- the audit's key bug) returns False."""
    assert _first_visible(_StubLoc(count=1, visible=True)) is True
    assert _first_visible(_StubLoc(count=2, visible=True)) is True
    assert _first_visible(_StubLoc(count=0)) is False
    # Audit case: count > 0 but not visible used to return True; now False.
    assert _first_visible(_StubLoc(count=1, visible=False)) is False


def test_legacy_first_visible_returns_false_on_strict_mode() -> None:
    """Strict-mode error used to be swallowed and converted to True
    via the count fallback. Now: returns False (failure state)."""
    assert (
        _first_visible(
            _StubLoc(count_raises=Exception("strict mode violation"))
        )
        is False
    )


def test_legacy_first_visible_returns_false_on_timeout() -> None:
    """Timeout used to be swallowed (the ``except: count() > 0`` fallback
    also re-raised inside count()). Now: timeout returns False."""
    assert (
        _first_visible(_StubLoc(count_raises=PWTimeoutError("count timed out")))
        is False
    )
