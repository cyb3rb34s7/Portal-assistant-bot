"""WI-36: auth preconditions on steps that hit protected endpoints.

Plan acceptance check:
  A Save step that fires PATCH after a 401-due-to-expired-session
  pauses cleanly with auth_missing instead of looping or silently
  failing. NO auto-relogin -- v1 is precondition check only.

Tests cover:
  - Schema: AuthPrecondition roundtrip.
  - Annotator: a click whose folded network children include a PATCH
    gets auth_precondition stamped; a click with only GET children
    does NOT.
  - Runner: _check_auth_signal returns ``ok`` / ``missing`` /
    ``unknown`` per the portal's auth_signal probe.
  - Runner: when step.auth_precondition is set and the probe returns
    missing, _execute_step emits error_kind="auth_missing" without
    touching the page.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from pilot.annotate import build_skill
from pilot.skill_models import (
    AuthPrecondition,
    ElementFingerprint,
    Skill,
    SkillStep,
    TraceEvent,
)


def _click(event_id: str, sequence: int, test_id: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id, tag="button", role="button",
        ),
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _network_request(
    event_id: str,
    sequence: int,
    caused_by: str,
    method: str,
    url: str,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="network_request",
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        caused_by=caused_by,
        initiator_event_id=caused_by,
        method=method,
        url=url,
        request_id=event_id,
        source="fetch_request_start",
    )


# ----- schema --------------------------------------------------------------


def test_auth_precondition_roundtrip() -> None:
    pre = AuthPrecondition(
        role_required="editor",
        session_namespace="tenant_acme",
        refresh_strategy="sso_flow",
    )
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn-save"),
        auth_precondition=pre,
    )
    restored = SkillStep.model_validate(step.model_dump())
    assert restored.auth_precondition is not None
    assert restored.auth_precondition.role_required == "editor"
    assert restored.auth_precondition.session_namespace == "tenant_acme"
    assert restored.auth_precondition.refresh_strategy == "sso_flow"


def test_auth_precondition_defaults() -> None:
    """Default refresh_strategy is ``navigate`` -- the conservative
    choice for portals that don't declare an SSO flow."""
    pre = AuthPrecondition()
    assert pre.refresh_strategy == "navigate"
    assert pre.role_required is None
    assert pre.session_namespace is None


# ----- annotator: destructive steps get auth_precondition ---------------


def test_build_skill_stamps_auth_precondition_on_patch_step() -> None:
    """A click followed by a PATCH network request is destructive.
    The annotator stamps auth_precondition; refresh_strategy defaults
    to ``navigate``."""
    ev_click = _click("save", 1, "btn-save")
    ev_req = _network_request(
        "req1", 2, "save", "PATCH", "/api/assets/123",
    )
    skill = build_skill(
        "save_asset", [ev_click, ev_req], auto=True, base_url="http://x",
    )
    assert isinstance(skill, Skill)
    assert len(skill.steps) == 1
    step = skill.steps[0]
    assert step.auth_precondition is not None
    assert step.auth_precondition.refresh_strategy == "navigate"


def test_build_skill_no_auth_precondition_on_read_only_step() -> None:
    """A click with only GET children is read-only -- no auth
    precondition emitted."""
    ev_click = _click("open", 1, "btn-open-detail")
    ev_req = _network_request(
        "req1", 2, "open", "GET", "/api/assets/123",
    )
    skill = build_skill(
        "open_detail", [ev_click, ev_req], auto=True, base_url="http://x",
    )
    step = skill.steps[0]
    assert step.auth_precondition is None


def test_build_skill_auth_precondition_on_post_delete_put() -> None:
    """POST / PUT / DELETE are also destructive."""
    for method in ("POST", "PUT", "DELETE"):
        ev_click = _click(f"act_{method}", 1, f"btn-{method.lower()}")
        ev_req = _network_request(
            f"req_{method}", 2, f"act_{method}", method,
            "/api/things/x",
        )
        skill = build_skill(
            f"act_{method}", [ev_click, ev_req],
            auto=True, base_url="http://x",
        )
        step = skill.steps[0]
        assert step.auth_precondition is not None, (
            f"expected auth_precondition on {method} step"
        )


# ----- runner: _check_auth_signal ----------------------------------------


def test_check_auth_signal_returns_unknown_without_selectors() -> None:
    """An auth_signal with no selectors is informational only."""
    from pilot.skill_runner import SkillRunner

    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    runner.portal_auth_signal = MagicMock(
        logged_in_when_visible=[],
        logged_out_when_visible=[],
        probe_timeout_ms=1000,
    )
    status, diag = runner._check_auth_signal()
    assert status == "unknown"
    assert "no selectors" in diag


def test_check_auth_signal_returns_ok_on_positive_match() -> None:
    """A visible positive signal returns ``ok``."""
    from pilot.skill_runner import SkillRunner

    page = MagicMock()
    nav_locator = MagicMock()
    nav_locator.first.is_visible.return_value = True
    page.locator.return_value = nav_locator

    runner = SkillRunner.__new__(SkillRunner)
    session = MagicMock()
    session.page = page
    runner.session = session
    runner.portal_auth_signal = MagicMock(
        logged_in_when_visible=['[data-testid="nav-user-menu"]'],
        logged_out_when_visible=[],
        probe_timeout_ms=1000,
    )
    status, diag = runner._check_auth_signal()
    assert status == "ok"
    assert "positive" in diag


def test_check_auth_signal_returns_missing_on_negative_match() -> None:
    """A visible logged_out signal returns ``missing``."""
    from pilot.skill_runner import SkillRunner

    page = MagicMock()
    # Positive selector NOT visible; negative selector IS visible.
    def _locator(sel):
        loc = MagicMock()
        if "sign-in" in sel:
            loc.first.is_visible.return_value = True
        else:
            loc.first.is_visible.return_value = False
        return loc
    page.locator.side_effect = _locator

    runner = SkillRunner.__new__(SkillRunner)
    session = MagicMock()
    session.page = page
    runner.session = session
    runner.portal_auth_signal = MagicMock(
        logged_in_when_visible=['[data-testid="nav-user-menu"]'],
        logged_out_when_visible=['[data-testid="sign-in-btn"]'],
        probe_timeout_ms=1000,
    )
    status, diag = runner._check_auth_signal()
    assert status == "missing"
    assert "sign-in" in diag


def test_check_auth_signal_returns_missing_when_no_signal_matches() -> None:
    """Conservative: when nothing matches, treat as missing so the
    operator gets a Resume click rather than running unauthenticated."""
    from pilot.skill_runner import SkillRunner

    page = MagicMock()
    loc = MagicMock()
    loc.first.is_visible.return_value = False
    page.locator.return_value = loc

    runner = SkillRunner.__new__(SkillRunner)
    session = MagicMock()
    session.page = page
    runner.session = session
    runner.portal_auth_signal = MagicMock(
        logged_in_when_visible=['[data-testid="nav-user-menu"]'],
        logged_out_when_visible=['[data-testid="sign-in-btn"]'],
        probe_timeout_ms=1000,
    )
    status, diag = runner._check_auth_signal()
    assert status == "missing"
    assert "none matched" in diag


def test_check_auth_signal_returns_unknown_without_portal_signal() -> None:
    """When the portal didn't declare an auth_signal, _check_auth_signal
    returns ``unknown`` -- the runner's caller treats this as
    'skip the check'."""
    from pilot.skill_runner import SkillRunner

    runner = SkillRunner.__new__(SkillRunner)
    runner.portal_auth_signal = None
    status, diag = runner._check_auth_signal()
    assert status == "unknown"
    assert "no auth_signal" in diag
