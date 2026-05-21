"""WI-35: popup / new-window workflows.

Plan acceptance check:
  "Open print preview" -> popup opens -> click button in popup ->
  popup closes -> main page workflow continues. The main step records
  the popup as an effect; the popup's interior steps are nested.

Tests cover:
  - Schema: PopupEffect carries page_binding_key + window_name +
    switch_policy and survives roundtrip.
  - TraceEvent kind="popup" carries popup_url, popup_target,
    popup_binding_key.
  - Annotator: a click followed by a popup event with caused_by ==
    click.event_id gets effects.popup populated with the
    binding_key from the popup event.
  - BrowserSession.popup_pages dict exists and defaults to empty.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import build_skill
from pilot.skill_models import (
    ElementFingerprint,
    PopupEffect,
    Skill,
    SkillStep,
    StepEffect,
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


def _popup_event(
    event_id: str,
    sequence: int,
    caused_by: str,
    popup_url: str,
    binding_key: str,
    target: str | None = None,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="popup",
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        caused_by=caused_by,
        initiator_event_id=caused_by,
        popup_url=popup_url,
        popup_target=target,
        popup_features=None,
        popup_binding_key=binding_key,
        source="window_open",
    )


# ----- schema --------------------------------------------------------------


def test_popup_effect_roundtrip() -> None:
    eff = PopupEffect(
        url_template=None,
        window_name="_blank",
        page_binding_key="popup_print_preview",
        switch_policy="switch",
        close_policy="explicit",
    )
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn-print-preview"),
        effects=StepEffect(popup=eff),
    )
    data = step.model_dump()
    restored = SkillStep.model_validate(data)
    assert restored.effects.popup.page_binding_key == "popup_print_preview"
    assert restored.effects.popup.window_name == "_blank"
    assert restored.effects.popup.switch_policy == "switch"


def test_trace_event_popup_kind_validates() -> None:
    """The TraceEvent kind Literal accepts ``popup`` events with the
    WI-35 payload fields populated."""
    ev = _popup_event(
        "p1", 1, "click1", "http://x/print", "popup_print_preview",
        target="_blank",
    )
    data = ev.model_dump()
    restored = TraceEvent.model_validate(data)
    assert restored.kind == "popup"
    assert restored.popup_url == "http://x/print"
    assert restored.popup_target == "_blank"
    assert restored.popup_binding_key == "popup_print_preview"


# ----- annotator: popup folds onto causing click --------------------------


def test_build_skill_folds_popup_effect_onto_click() -> None:
    ev_click = _click("click1", 1, "btn-print-preview")
    ev_popup = _popup_event(
        "p1", 2, "click1", "http://x/print", "popup_print_preview",
        target="_blank",
    )
    skill = build_skill(
        "open_print_preview",
        [ev_click, ev_popup],
        auto=True,
        base_url="http://x",
    )
    assert isinstance(skill, Skill)
    # The popup event is observed; only the click becomes a step.
    actions = [s.action for s in skill.steps]
    assert actions == ["click"]
    click_step = skill.steps[0]
    assert click_step.effects is not None
    assert click_step.effects.popup is not None
    assert click_step.effects.popup.page_binding_key == "popup_print_preview"
    assert click_step.effects.popup.window_name == "_blank"
    # Provenance carries the popup event id so the audit trail is
    # complete.
    prov = click_step.provenance
    assert prov is not None
    assert "click1" in prov.raw_event_ids
    # The popup event is folded into the cluster -- it appears in the
    # cluster's raw_event_ids list.
    assert "p1" in prov.raw_event_ids


# ----- BrowserSession registry ---------------------------------------------


def test_browser_session_has_popup_pages_dict() -> None:
    """WI-35: BrowserSession exposes a popup_pages dict that the
    runner populates when a click captures a new tab."""
    from pilot.browser import BrowserSession
    # Construct with positional core fields; popup_pages defaults.
    # We can't reach real Playwright here, but the dataclass itself
    # is the contract under test.
    s = BrowserSession(
        playwright=None,  # type: ignore[arg-type]
        browser=None,  # type: ignore[arg-type]
        context=None,  # type: ignore[arg-type]
        page=None,  # type: ignore[arg-type]
    )
    assert s.popup_pages == {}
    s.popup_pages["popup_test"] = object()
    assert "popup_test" in s.popup_pages
