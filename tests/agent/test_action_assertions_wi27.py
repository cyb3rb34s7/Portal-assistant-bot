"""Tests for WI-27: action-specific postconditions replace L3
verification page signature.

Pre-WI-27, ``_execute_with_heal_check`` compared a whole-page
signature ``(url, body innerText length, count of interactables)``
before vs after a healed action. The audit pointed out:
  - spinner text change passes a wrong click,
  - silent save fails verification despite the click being correct,
  - unrelated SPA route changes flip the verifier on a click that
    didn't navigate.

WI-27 extends ``StepAssertion`` with action-specific kinds and routes
verification through them when declared:
  - url_matches_template
  - field_value_equals
  - selection_equals
  - toast_visible
  - request_completed
  - download_started (placeholder until WI-45)

Persistence of L3 heals onto the skill JSON is now gated on the
declared assertions passing -- not the legacy page-signature diff.

Acceptance check (from the plan):
  L3 click that changes unrelated text but not the expected field /
  status fails verification.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

import pytest

from pilot.skill_models import (
    Skill,
    SkillStep,
    StepAssertion,
)
from pilot.skill_runner import SkillRunner


# ---------------------------------------------------------------------
# Schema additions: action-specific assertion kinds + fields
# ---------------------------------------------------------------------


def test_assertion_url_matches_template_accepts_url_template_field() -> None:
    """The new url_matches_template kind carries a url_template field
    that the runner renders through params before comparing."""
    a = StepAssertion(
        kind="url_matches_template",
        url_template="/asset/{content_id}",
    )
    assert a.kind == "url_matches_template"
    assert a.url_template == "/asset/{content_id}"


def test_assertion_field_value_equals_carries_selector_and_text() -> None:
    a = StepAssertion(
        kind="field_value_equals",
        selector="[data-testid='input-title']",
        text="New Title",
    )
    assert a.kind == "field_value_equals"
    assert a.selector == "[data-testid='input-title']"
    assert a.text == "New Title"


def test_assertion_selection_equals_carries_selector_and_text() -> None:
    a = StepAssertion(
        kind="selection_equals",
        selector="[data-testid='select-region']",
        text="APAC",
    )
    assert a.kind == "selection_equals"
    assert a.text == "APAC"


def test_assertion_toast_visible_optional_selector_and_text() -> None:
    """toast_visible accepts an optional selector (defaults to
    [role='status'] / [role='alert']) and an optional text substring
    matcher."""
    a = StepAssertion(kind="toast_visible", text="Saved")
    assert a.kind == "toast_visible"
    assert a.text == "Saved"
    # selector left None means: use the default role-based fallback
    assert a.selector is None


def test_assertion_request_completed_carries_pattern_and_status() -> None:
    a = StepAssertion(
        kind="request_completed",
        request_pattern="/api/assets/A-9001",
        expected_status=200,
    )
    assert a.kind == "request_completed"
    assert a.request_pattern == "/api/assets/A-9001"
    assert a.expected_status == 200


def test_assertion_download_started_carries_filename_pattern() -> None:
    a = StepAssertion(
        kind="download_started",
        filename_pattern="report-{asset_id}.csv",
    )
    assert a.kind == "download_started"
    assert a.filename_pattern == "report-{asset_id}.csv"


# ---------------------------------------------------------------------
# Runner: _check_one_assertion handles the new kinds without crashing
# ---------------------------------------------------------------------


class _StubPage:
    """Minimal page stub that lets us drive _check_one_assertion for
    the new assertion kinds. Only the methods actually touched are
    implemented; pytest will raise on anything else."""

    def __init__(
        self,
        *,
        url: str = "",
        input_value: str = "",
        selection_value: str = "",
        toast_text: str | None = None,
        toast_visible: bool = False,
        request_log: list[dict] | None = None,
    ):
        self._url = url
        self._input_value = input_value
        self._selection_value = selection_value
        self._toast_text = toast_text
        self._toast_visible = toast_visible
        self._request_log = request_log or []

    @property
    def url(self) -> str:
        return self._url

    def locator(self, selector: str):
        page = self
        class _Loc:
            def __init__(self):
                self.first = self
            def wait_for(self, state: str, timeout: int = 4000):
                if state == "visible" and not page._toast_visible:
                    raise RuntimeError("not visible")
            def inner_text(self, timeout: int = 4000) -> str:
                return page._toast_text or ""
            def input_value(self, timeout: int = 4000) -> str:
                return page._input_value
            def evaluate(self, expr: str):
                if "value" in expr:
                    return page._selection_value
                return None
        return _Loc()

    def evaluate(self, expr: str, *args):
        # request_completed predicate. Simulate the in-page log scan.
        if "__cp_request_log" in expr:
            pat, status_filter = args[0] if args else ("", None)
            for r in self._request_log:
                if pat.lower() not in (r.get("url") or "").lower():
                    continue
                if r.get("finished_ts", 0) <= 0:
                    continue
                status = r.get("status", 0)
                if status_filter is None:
                    if 200 <= status < 300:
                        return True
                else:
                    if status == status_filter:
                        return True
            return False
        return None


def _runner_with_params(params: dict[str, str] | None = None) -> SkillRunner:
    skill = Skill(
        name="t",
        steps=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    return SkillRunner(
        session=None,  # type: ignore[arg-type]
        skill=skill,
        params=params or {},
        sessions_dir=Path(tempfile.gettempdir()),
    )


def test_check_url_matches_template_renders_through_params() -> None:
    runner = _runner_with_params({"content_id": "A-9001"})
    a = StepAssertion(
        kind="url_matches_template",
        url_template="/asset/{content_id}",
    )
    assert (
        runner._check_one_assertion(
            _StubPage(url="https://portal.local/asset/A-9001"),  # type: ignore[arg-type]
            a,
        )
        is True
    )
    # Wrong asset id -- the rendered template doesn't appear in URL.
    assert (
        runner._check_one_assertion(
            _StubPage(url="https://portal.local/asset/B-12345"),  # type: ignore[arg-type]
            a,
        )
        is False
    )


def test_check_field_value_equals_compares_input_value() -> None:
    runner = _runner_with_params()
    a = StepAssertion(
        kind="field_value_equals",
        selector="[data-testid='input-title']",
        text="New Title",
    )
    assert (
        runner._check_one_assertion(_StubPage(input_value="New Title"), a)  # type: ignore[arg-type]
        is True
    )
    assert (
        runner._check_one_assertion(_StubPage(input_value="Old Title"), a)  # type: ignore[arg-type]
        is False
    )


def test_check_selection_equals_reads_dom_value() -> None:
    runner = _runner_with_params()
    a = StepAssertion(
        kind="selection_equals",
        selector="[data-testid='select-region']",
        text="APAC",
    )
    assert (
        runner._check_one_assertion(_StubPage(selection_value="APAC"), a)  # type: ignore[arg-type]
        is True
    )
    assert (
        runner._check_one_assertion(_StubPage(selection_value="EMEA"), a)  # type: ignore[arg-type]
        is False
    )


def test_check_toast_visible_with_text_match() -> None:
    runner = _runner_with_params()
    a = StepAssertion(kind="toast_visible", text="Saved")
    # Toast not visible -- assertion fails.
    assert (
        runner._check_one_assertion(_StubPage(toast_visible=False), a)  # type: ignore[arg-type]
        is False
    )
    # Toast visible but text doesn't match.
    assert (
        runner._check_one_assertion(
            _StubPage(toast_visible=True, toast_text="Error: ..."),  # type: ignore[arg-type]
            a,
        )
        is False
    )
    # Toast visible and text matches.
    assert (
        runner._check_one_assertion(
            _StubPage(toast_visible=True, toast_text="Saved successfully"),  # type: ignore[arg-type]
            a,
        )
        is True
    )


def test_check_request_completed_finds_matching_log_entry() -> None:
    runner = _runner_with_params()
    a = StepAssertion(
        kind="request_completed",
        request_pattern="/api/assets/A-9001",
        expected_status=200,
    )
    # Log empty -- fail.
    assert (
        runner._check_one_assertion(_StubPage(request_log=[]), a)  # type: ignore[arg-type]
        is False
    )
    # Log has the request -- pass.
    assert (
        runner._check_one_assertion(
            _StubPage(
                request_log=[
                    {
                        "url": "/api/assets/A-9001",
                        "status": 200,
                        "finished_ts": 1000,
                    }
                ],
            ),  # type: ignore[arg-type]
            a,
        )
        is True
    )
    # Log has different request -- fail.
    assert (
        runner._check_one_assertion(
            _StubPage(
                request_log=[
                    {
                        "url": "/api/markets",
                        "status": 200,
                        "finished_ts": 1000,
                    }
                ],
            ),  # type: ignore[arg-type]
            a,
        )
        is False
    )


def test_check_request_completed_status_none_means_any_2xx() -> None:
    runner = _runner_with_params()
    a = StepAssertion(
        kind="request_completed",
        request_pattern="/api/assets",
        expected_status=None,
    )
    # 200 -- pass.
    assert (
        runner._check_one_assertion(
            _StubPage(
                request_log=[
                    {"url": "/api/assets", "status": 200, "finished_ts": 1000}
                ],
            ),  # type: ignore[arg-type]
            a,
        )
        is True
    )
    # 201 -- still any-2xx, pass.
    assert (
        runner._check_one_assertion(
            _StubPage(
                request_log=[
                    {"url": "/api/assets", "status": 201, "finished_ts": 1000}
                ],
            ),  # type: ignore[arg-type]
            a,
        )
        is True
    )
    # 400 -- not 2xx, fail.
    assert (
        runner._check_one_assertion(
            _StubPage(
                request_log=[
                    {"url": "/api/assets", "status": 400, "finished_ts": 1000}
                ],
            ),  # type: ignore[arg-type]
            a,
        )
        is False
    )


def test_check_download_started_requires_captured_download_after_wi45() -> None:
    """WI-45 wired the ``download_started`` assertion to verify a real
    captured download recorded by _do_download instead of returning the
    pre-WI-45 placeholder True. Without a captured download the
    assertion FAILS (the action that should have downloaded didn't);
    with one it succeeds, and a declared filename_pattern is matched
    case-insensitively against the captured suggested filename."""
    runner = _runner_with_params()
    a = StepAssertion(kind="download_started")
    # No prior download -> assertion fails (was True placeholder pre-WI-45).
    assert runner._check_one_assertion(_StubPage(), a) is False  # type: ignore[arg-type]
    # Simulate a captured download.
    runner._last_download = {
        "suggested": "export-2026-05-22.csv",
        "saved_path": "/tmp/sessions/x/downloads/export-2026-05-22.csv",
        "size": 1024,
    }
    assert runner._check_one_assertion(_StubPage(), a) is True  # type: ignore[arg-type]
    # filename_pattern substring match (case-insensitive).
    a_with_pat = StepAssertion(
        kind="download_started",
        filename_pattern=".CSV",
    )
    assert runner._check_one_assertion(_StubPage(), a_with_pat) is True  # type: ignore[arg-type]
    a_mismatch = StepAssertion(
        kind="download_started",
        filename_pattern="report.pdf",
    )
    assert runner._check_one_assertion(_StubPage(), a_mismatch) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# Heal verification routing: action-specific assertions WIN over the
# legacy whole-page-signature path.
# ---------------------------------------------------------------------


def test_step_with_assertions_does_not_use_legacy_signature() -> None:
    """When a step declares assert_after, _execute_with_heal_check
    routes through the structured verifier. The signature path is
    only the back-compat fallback for skills without assertions."""
    runner = _runner_with_params()
    step = SkillStep(
        index=0,
        action="click",
        assert_after=[
            StepAssertion(
                kind="url_matches_template",
                url_template="/asset/A-9001",
            )
        ],
    )
    # Stubbed page where:
    #   - the post-action URL DOES match the assertion's template,
    #   - but the legacy signature wouldn't have differed (we never
    #     call _page_state_signature in this path).
    page = _StubPage(url="https://portal.local/asset/A-9001")
    called_signature = {"flag": False}

    def _spy_sig(page):
        called_signature["flag"] = True
        return ("", 0, 0)

    runner._page_state_signature = _spy_sig  # type: ignore[assignment]

    heal_info: dict = {}
    ok = runner._execute_with_heal_check(
        page,  # type: ignore[arg-type]
        level=3,
        heal_info=heal_info,
        action_callable=lambda: None,
        step=step,
    )
    assert ok is True
    # The legacy page-signature path must NOT have been called.
    assert called_signature["flag"] is False
    assert heal_info["verification_method"] == "assert_after"


def test_step_without_assertions_falls_back_to_page_signature() -> None:
    """Skills authored before WI-27 (no assert_after) still get the
    legacy whole-page-signature verifier. Back-compat preserved."""
    runner = _runner_with_params()
    step = SkillStep(index=0, action="click")  # no assert_after

    # Spy on the signature path to confirm it's called.
    sig_calls = {"count": 0}

    def _spy_sig(page):
        sig_calls["count"] += 1
        return ("u", sig_calls["count"], 0)

    runner._page_state_signature = _spy_sig  # type: ignore[assignment]
    heal_info: dict = {}
    page = _StubPage()
    ok = runner._execute_with_heal_check(
        page,  # type: ignore[arg-type]
        level=3,
        heal_info=heal_info,
        action_callable=lambda: None,
        step=step,
    )
    # Signature called twice -- before and after -- and they differ
    # because the spy increments count; passed=True.
    assert sig_calls["count"] == 2
    assert ok is True
    assert heal_info["verification_method"] == "page_signature_legacy"


def test_failing_assertion_records_failure_detail() -> None:
    """When an assertion fails, the heal_info records the specific
    failing assertion so the operator (and the audit) sees WHICH
    postcondition the heal violated -- not just 'verify failed'."""
    runner = _runner_with_params()
    step = SkillStep(
        index=0,
        action="click",
        assert_after=[
            StepAssertion(
                kind="url_matches_template",
                url_template="/expected-route",
            )
        ],
    )
    page = _StubPage(url="https://portal.local/totally-different")
    heal_info: dict = {}
    ok = runner._execute_with_heal_check(
        page,  # type: ignore[arg-type]
        level=3,
        heal_info=heal_info,
        action_callable=lambda: None,
        step=step,
    )
    assert ok is False
    assert heal_info["verification_method"] == "assert_after"
    assert "failed_assertion" in heal_info
    assert "url_matches_template" in heal_info["failed_assertion"]
