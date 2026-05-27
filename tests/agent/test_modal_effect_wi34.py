"""WI-34: modal/dialog effects on click steps.

Plan acceptance check:
  A "confirm publish" modal records as ONE step with:
    click-Publish + dialog-appears + click-Yes
  Replay waits for the dialog before clicking Yes and verifies
  dismissal.

Tests cover:
  - Schema roundtrip: ModalEffect with opens_on_action /
    closes_on_action / close_actions persists.
  - Schema backward-compat: legacy ModalEffect with kind="open" still
    validates.
  - Annotator: a click that produced a modal-open observation gets an
    effects.modal with opens_on_action=True populated.
  - Annotator: a subsequent click inside the dialog that produced a
    modal-closed observation gets effects.modal.closes_on_action=True
    with a close_actions[0] of kind="click" + target_fp.
  - Annotator: in-dialog steps get ambiguity_policy.locator_scope set
    to the dialog selector so the runner narrows lookups inside the
    modal.
  - Annotator: a global Escape that closed the dialog produces a
    close_actions[0] of kind="escape".
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _click_inside_dialog,
    _extract_testid_from_selector,
    _index_modal_effects,
    _assign_synthetic_ids,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    ModalCloseAction,
    ModalEffect,
    Skill,
    SkillStep,
    StepEffect,
    TraceEvent,
)


def _click(
    event_id: str,
    sequence: int,
    test_id: str,
    *,
    ancestor_dialog_testid: str | None = None,
) -> TraceEvent:
    chain = []
    if ancestor_dialog_testid:
        chain = [{
            "tag": "div",
            "id": None,
            "testId": ancestor_dialog_testid,
            "role": "dialog",
            "className": None,
        }]
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="button",
            role="button",
            ancestor_chain=chain,
        ),
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _modal_event(
    event_id: str,
    sequence: int,
    state: str,
    selector: str,
    caused_by: str | None,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="modal",
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        caused_by=caused_by,
        initiator_event_id=caused_by,
        dialog_state=state,  # type: ignore[arg-type]
        dialog_selector=selector,
        dialog_aria_modal=True,
        source="dialog_observer",
    )


def _key(
    event_id: str,
    sequence: int,
    key: str,
    test_id: str = "body",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="key",
        fingerprint=ElementFingerprint(test_id=test_id, tag="body"),
        value=key,
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_keydown",
    )


# ----- schema --------------------------------------------------------------


def test_modal_effect_schema_wi34_fields_roundtrip() -> None:
    eff = ModalEffect(
        opens_on_action=True,
        closes_on_action=True,
        dialog_selector='[data-testid="modal-publish-confirm"]',
        close_actions=[
            ModalCloseAction(
                kind="click",
                target_fp=ElementFingerprint(test_id="btn-publish-yes"),
            ),
        ],
    )
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn-publish"),
        effects=StepEffect(modal=eff),
    )
    data = step.model_dump()
    restored = SkillStep.model_validate(data)
    assert restored.effects is not None
    assert restored.effects.modal is not None
    assert restored.effects.modal.opens_on_action is True
    assert restored.effects.modal.closes_on_action is True
    assert (
        restored.effects.modal.dialog_selector
        == '[data-testid="modal-publish-confirm"]'
    )
    assert len(restored.effects.modal.close_actions) == 1
    assert restored.effects.modal.close_actions[0].kind == "click"
    ca_fp = restored.effects.modal.close_actions[0].target_fp
    assert ca_fp is not None and ca_fp.test_id == "btn-publish-yes"


def test_modal_effect_legacy_shape_still_validates() -> None:
    """Pre-WI-34 ModalEffect carried kind="open"/"close" only. Those
    skills must still load so we don't break legacy traces."""
    eff = ModalEffect(
        kind="open",
        dialog_test_id="modal-publish-confirm",
        expected_visibility=True,
    )
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn-publish"),
        effects=StepEffect(modal=eff),
    )
    data = step.model_dump()
    restored = SkillStep.model_validate(data)
    assert restored.effects.modal.kind == "open"
    assert restored.effects.modal.dialog_test_id == "modal-publish-confirm"
    # New fields default to False/empty.
    assert restored.effects.modal.opens_on_action is False
    assert restored.effects.modal.closes_on_action is False


# ----- helpers -------------------------------------------------------------


def test_extract_testid_from_selector_recognizes_testid() -> None:
    assert _extract_testid_from_selector(
        '[data-testid="modal-publish-confirm"]'
    ) == "modal-publish-confirm"
    assert _extract_testid_from_selector(
        '[role="dialog"]:nth-of-type(1)'
    ) is None


def test_click_inside_dialog_detects_role_dialog_ancestor() -> None:
    ev = _click(
        "c1", 1, "btn-yes", ancestor_dialog_testid="modal-confirm",
    )
    assert _click_inside_dialog(
        ev, '[data-testid="modal-confirm"]'
    ) is True


# ----- annotator: open + close folded ------------------------------------


def test_index_modal_effects_open_then_inner_click_close() -> None:
    """
    Scenario: click Publish -> dialog opens -> click Yes inside dialog ->
    dialog closes.
    Expected:
      - The Publish click's ModalEffect has opens_on_action=True.
      - The Yes click's ModalEffect has closes_on_action=True with a
        close_actions[0] of kind="click" + target_fp == the Yes button.
      - The Yes click is in modal_scope_by_event (so the annotator
        narrows its locator scope inside the dialog).
    """
    ev_publish = _click("publish", 1, "btn-publish")
    ev_open = _modal_event(
        "modal_open", 2, "open",
        '[data-testid="modal-publish-confirm"]',
        caused_by="publish",
    )
    ev_yes = _click(
        "yes", 3, "btn-publish-yes",
        ancestor_dialog_testid="modal-publish-confirm",
    )
    ev_close = _modal_event(
        "modal_close", 4, "closed",
        '[data-testid="modal-publish-confirm"]',
        caused_by="yes",
    )
    events = [ev_publish, ev_open, ev_yes, ev_close]
    events = _assign_synthetic_ids(events)
    causality = build_causality_graph(events)
    effects_by_cause, scope_by_event = _index_modal_effects(
        events, causality,
    )
    assert "publish" in effects_by_cause
    assert effects_by_cause["publish"].opens_on_action is True
    assert (
        effects_by_cause["publish"].dialog_selector
        == '[data-testid="modal-publish-confirm"]'
    )
    assert "yes" in effects_by_cause
    assert effects_by_cause["yes"].closes_on_action is True
    assert len(effects_by_cause["yes"].close_actions) == 1
    ca = effects_by_cause["yes"].close_actions[0]
    assert ca.kind == "click"
    assert ca.target_fp is not None
    assert ca.target_fp.test_id == "btn-publish-yes"
    # The Yes click happened while the dialog was open -> scoped.
    assert (
        scope_by_event.get("yes")
        == '[data-testid="modal-publish-confirm"]'
    )


def test_index_modal_effects_escape_closes_dialog() -> None:
    """Global Escape with a dialog open produces close_actions[0]
    of kind='escape'."""
    ev_open_click = _click("open", 1, "btn-open-modal")
    ev_open = _modal_event(
        "m_open", 2, "open",
        '[data-testid="modal-x"]', caused_by="open",
    )
    ev_escape = _key("escape", 3, "Escape", test_id="body")
    ev_close = _modal_event(
        "m_close", 4, "closed",
        '[data-testid="modal-x"]', caused_by="escape",
    )
    events = _assign_synthetic_ids(
        [ev_open_click, ev_open, ev_escape, ev_close]
    )
    causality = build_causality_graph(events)
    effects_by_cause, _ = _index_modal_effects(events, causality)
    assert "escape" in effects_by_cause
    assert effects_by_cause["escape"].closes_on_action is True
    assert len(effects_by_cause["escape"].close_actions) == 1
    assert effects_by_cause["escape"].close_actions[0].kind == "escape"
    assert effects_by_cause["escape"].close_actions[0].target_fp is None


def test_index_modal_effects_backdrop_click() -> None:
    """A click whose ancestor chain doesn't include the dialog gets
    classified as backdrop."""
    ev_open_click = _click("open", 1, "btn-open-modal")
    ev_open = _modal_event(
        "m_open", 2, "open",
        '[data-testid="modal-x"]', caused_by="open",
    )
    # backdrop click -- NO dialog ancestor.
    ev_backdrop = _click("backdrop", 3, "page-bg")
    ev_close = _modal_event(
        "m_close", 4, "closed",
        '[data-testid="modal-x"]', caused_by="backdrop",
    )
    events = _assign_synthetic_ids(
        [ev_open_click, ev_open, ev_backdrop, ev_close]
    )
    causality = build_causality_graph(events)
    effects_by_cause, _ = _index_modal_effects(events, causality)
    assert effects_by_cause["backdrop"].closes_on_action is True
    assert (
        effects_by_cause["backdrop"].close_actions[0].kind == "backdrop"
    )
    assert effects_by_cause["backdrop"].close_actions[0].target_fp is None


# ----- annotator: build_skill produces the modal step ---------------------


def test_build_skill_folds_modal_effect_onto_publish_click() -> None:
    """End-to-end: build_skill consumes the trace and produces a step
    list where the publish click carries the ModalEffect."""
    ev_publish = _click("publish", 1, "btn-publish")
    ev_open = _modal_event(
        "modal_open", 2, "open",
        '[data-testid="modal-publish-confirm"]',
        caused_by="publish",
    )
    ev_yes = _click(
        "yes", 3, "btn-publish-yes",
        ancestor_dialog_testid="modal-publish-confirm",
    )
    ev_close = _modal_event(
        "modal_close", 4, "closed",
        '[data-testid="modal-publish-confirm"]',
        caused_by="yes",
    )
    events = [ev_publish, ev_open, ev_yes, ev_close]
    skill = build_skill(
        "confirm_publish", events, auto=True, base_url="http://x",
    )
    # Two user-action steps: publish and yes. modal events are observed
    # and don't produce their own steps.
    assert isinstance(skill, Skill)
    actions = [s.action for s in skill.steps]
    assert actions == ["click", "click"]
    publish_step, yes_step = skill.steps
    assert publish_step.effects is not None
    assert publish_step.effects.modal is not None
    assert publish_step.effects.modal.opens_on_action is True
    assert yes_step.effects is not None
    assert yes_step.effects.modal is not None
    assert yes_step.effects.modal.closes_on_action is True
    # In-dialog ambiguity scope.
    assert yes_step.ambiguity_policy is not None
    assert (
        yes_step.ambiguity_policy.locator_scope
        == '[data-testid="modal-publish-confirm"]'
    )
