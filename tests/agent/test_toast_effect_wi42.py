"""WI-42: toast / notification driven undo + conflict.

Plan acceptance check:
  - A Save that produces "Saved" toast confirms success structurally.
  - A "Save failed: conflict" toast fails the step with that text in
    error_details.

Tests cover the deterministic pipeline (no live browser):
  - Schema: ToastEffect roundtrip with the WI-42 fields (level,
    text_pattern, dismiss_strategy, dependent_actions).
  - Schema: TraceEvent.kind accepts 'toast'; toast_text / toast_level
    / toast_selector / toast_action_buttons optional.
  - Annotator: a toast event whose initiator_event_id matches a
    click step gets folded into step.effects.toast.
  - Annotator: undo-style action buttons surface as
    dependent_actions; dismiss_strategy='click_action' when actionable
    buttons are present.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import build_skill
from pilot.skill_models import (
    ElementFingerprint,
    SkillStep,
    StepEffect,
    ToastEffect,
    TraceEvent,
)


def _save_click_event(
    event_id: str = "save1",
    sequence: int = 1,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id="btn-save",
            tag="button",
            text="Save",
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


def _toast_event(
    event_id: str,
    caused_by: str,
    sequence: int,
    *,
    text: str = "Saved",
    level: str = "success",
    buttons: list[dict] | None = None,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="toast",
        page_url="http://x",
        event_id=event_id,
        interaction_id=caused_by,
        caused_by=caused_by,
        initiator_event_id=caused_by,
        sequence=sequence,
        source="toast_observer",
        raw_event_kind="toast_appeared",
        toast_text=text,
        toast_level=level,  # type: ignore[arg-type]
        toast_selector=f"[data-testid='toast-{level}']",
        toast_action_buttons=buttons,
    )


# ----- schema --------------------------------------------------------------


def test_toast_effect_roundtrip_with_wi42_fields() -> None:
    eff = ToastEffect(
        level="error",
        text_pattern="Save failed",
        dismiss_strategy="click_action",
        dependent_actions=[
            {"label": "Retry", "test_id": "toast-retry", "action_kind": "retry"}
        ],
    )
    payload = eff.model_dump()
    restored = ToastEffect(**payload)
    assert restored.level == "error"
    assert restored.text_pattern == "Save failed"
    assert restored.dismiss_strategy == "click_action"
    assert len(restored.dependent_actions) == 1
    assert restored.dependent_actions[0]["action_kind"] == "retry"


def test_step_effect_carries_toast() -> None:
    se = StepEffect(toast=ToastEffect(level="success", text_pattern="Saved"))
    payload = se.model_dump()
    restored = StepEffect(**payload)
    assert restored.toast is not None
    assert restored.toast.level == "success"


def test_trace_event_accepts_toast_kind() -> None:
    ev = _toast_event("t1", "c1", 2, text="Saved", level="success")
    assert ev.kind == "toast"
    assert ev.toast_text == "Saved"
    assert ev.toast_level == "success"


# ----- annotator folding ---------------------------------------------------


def test_save_toast_folds_into_click_effects() -> None:
    save = _save_click_event("save1", 1)
    toast = _toast_event("t1", caused_by="save1", sequence=2, text="Saved")
    skill = build_skill(
        skill_name="save_with_toast",
        events=[save, toast],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    click_steps = [s for s in skill.steps if s.action == "click"]
    assert len(click_steps) == 1
    cs = click_steps[0]
    assert cs.effects is not None
    assert cs.effects.toast is not None
    assert cs.effects.toast.level == "success"
    assert cs.effects.toast.text_pattern == "Saved"


def test_error_toast_level_propagates() -> None:
    save = _save_click_event("save1", 1)
    toast = _toast_event(
        "t1", caused_by="save1", sequence=2,
        text="Save failed: conflict", level="error",
    )
    skill = build_skill(
        skill_name="save_with_error",
        events=[save, toast],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    cs = [s for s in skill.steps if s.action == "click"][0]
    assert cs.effects is not None
    assert cs.effects.toast is not None
    assert cs.effects.toast.level == "error"
    assert "conflict" in (cs.effects.toast.text_pattern or "")


def test_undo_buttons_become_dependent_actions() -> None:
    save = _save_click_event("save1", 1)
    toast = _toast_event(
        "t1", caused_by="save1", sequence=2,
        text="Saved (Undo)", level="success",
        buttons=[
            {"label": "Undo", "test_id": "toast-undo", "action_kind": "undo"},
        ],
    )
    skill = build_skill(
        skill_name="save_with_undo",
        events=[save, toast],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    cs = [s for s in skill.steps if s.action == "click"][0]
    assert cs.effects is not None
    assert cs.effects.toast is not None
    assert cs.effects.toast.dismiss_strategy == "click_action"
    assert len(cs.effects.toast.dependent_actions) == 1
    assert cs.effects.toast.dependent_actions[0]["action_kind"] == "undo"


def test_toast_event_does_not_produce_standalone_step() -> None:
    # A toast event without a paired user action is observational only
    # and must not become a step.
    orphan_toast = TraceEvent(
        ts=datetime.utcnow(),
        kind="toast",
        page_url="http://x",
        event_id="t_orphan",
        interaction_id="t_orphan",
        caused_by=None,
        initiator_event_id=None,
        sequence=1,
        source="toast_observer",
        raw_event_kind="toast_appeared",
        toast_text="A background toast",
        toast_level="info",
    )
    skill = build_skill(
        skill_name="orphan_toast",
        events=[orphan_toast],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    # No steps produced; the toast is observational with no cause.
    assert skill.steps == []
