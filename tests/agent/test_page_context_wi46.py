"""WI-46: cross-tab / multi-window workflows.

Plan acceptance check:
  A print-preview workflow records main-page Click -> popup Click ->
  main-page Save and replays in the same page topology.

Tests cover:
  - PageContext schema roundtrip.
  - Annotator tags steps that lived inside a popup with page_context
    pointing at the popup's binding_key (from WI-35 popup capture).
  - SkillRunner._page_for_step routes to the registered popup page
    when page_context is set, and falls back to session.page when
    the binding is missing (with a structured diagnostic).
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import build_skill
from pilot.skill_models import (
    ElementFingerprint,
    PageContext,
    Skill,
    SkillStep,
    TraceEvent,
)


def _click(
    event_id: str,
    sequence: int,
    test_id: str,
    page_url: str = "http://main",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id, tag="button", role="button",
        ),
        page_url=page_url,
        event_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _popup_event(
    event_id: str,
    sequence: int,
    caused_by: str,
    popup_url: str,
    binding_key: str,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="popup",
        page_url="http://main",
        event_id=event_id,
        sequence=sequence,
        caused_by=caused_by,
        initiator_event_id=caused_by,
        popup_url=popup_url,
        popup_target="_blank",
        popup_features=None,
        popup_binding_key=binding_key,
        source="window_open",
    )


# ----- schema --------------------------------------------------------------


def test_page_context_roundtrip() -> None:
    ctx = PageContext(
        page_binding_key="popup_print_preview",
        opens_via="popup_event",
        expected_close="auto",
    )
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn-in-popup"),
        page_context=ctx,
    )
    data = step.model_dump()
    restored = SkillStep.model_validate(data)
    assert restored.page_context is not None
    assert restored.page_context.page_binding_key == "popup_print_preview"
    assert restored.page_context.expected_close == "auto"


# ----- annotator: tag steps inside popups ---------------------------------


def test_annotator_stamps_page_context_on_popup_interior_steps() -> None:
    """A workflow: main click opens popup, click inside popup, then
    click back on main. The middle step's page_context.page_binding_key
    should equal the popup's binding_key; the bracketing steps should
    have None."""
    ev_main_open = _click("c1", 1, "btn-open-preview", page_url="http://main")
    ev_popup = _popup_event("p1", 2, "c1", "http://preview", "popup_print")
    ev_in_popup = _click("c2", 3, "btn-print", page_url="http://preview")
    ev_main_back = _click("c3", 4, "btn-save", page_url="http://main")
    skill = build_skill(
        "print_preview",
        [ev_main_open, ev_popup, ev_in_popup, ev_main_back],
        auto=True,
        base_url="http://main",
    )
    assert isinstance(skill, Skill)
    assert [s.action for s in skill.steps] == ["click", "click", "click"]
    s_open, s_popup_click, s_save = skill.steps
    # Opening click stays on main page -- no page_context.
    assert s_open.page_context is None
    # Popup-interior click is tagged with the binding key.
    assert s_popup_click.page_context is not None
    assert s_popup_click.page_context.page_binding_key == "popup_print"
    # Main-page click after the popup -- back on main page.
    assert s_save.page_context is None


# ----- runner routing ------------------------------------------------------


def test_runner_page_for_step_routes_to_popup() -> None:
    """SkillRunner._page_for_step returns BrowserSession.popup_pages[binding]
    when the step declares page_context. Falls back to main page when
    the binding isn't registered (with a runner.page_context_unbound
    diagnostic)."""
    from pilot.browser import BrowserSession
    from pilot.skill_runner import SkillRunner
    from pathlib import Path
    import tempfile

    main = object()
    popup = object()
    session = BrowserSession(
        playwright=None,  # type: ignore[arg-type]
        browser=None,  # type: ignore[arg-type]
        context=None,  # type: ignore[arg-type]
        page=main,  # type: ignore[arg-type]
    )
    session.popup_pages["popup_print"] = popup
    skill = Skill(name="x", steps=[
        SkillStep(index=0, action="click"),
    ])
    with tempfile.TemporaryDirectory() as td:
        runner = SkillRunner(
            session=session,
            skill=skill,
            params={},
            sessions_dir=Path(td),
        )
    # No page_context -> main.
    bare_step = SkillStep(index=0, action="click")
    assert runner._page_for_step(bare_step) is main
    # page_context with valid binding -> popup.
    routed_step = SkillStep(
        index=1,
        action="click",
        page_context=PageContext(
            page_binding_key="popup_print",
            expected_close="auto",
        ),
    )
    assert runner._page_for_step(routed_step) is popup
    # page_context with unknown binding -> fallback main + diagnostic.
    runner.diagnostics.clear()
    miss_step = SkillStep(
        index=2,
        action="click",
        page_context=PageContext(
            page_binding_key="popup_does_not_exist",
            expected_close="auto",
        ),
    )
    assert runner._page_for_step(miss_step) is main
    assert any(
        d.code == "runner.page_context_unbound" for d in runner.diagnostics
    )
