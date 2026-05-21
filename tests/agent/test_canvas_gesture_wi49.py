"""WI-49: canvas / SVG / media adapter-driven gestures.

Plan acceptance check:
  A ``canvas_gesture`` step with adapter_name='noop_click' records +
  replays. The bundled NoOp adapter dispatches a click at the center
  of the target descriptor's selector so the schema + dispatch path is
  tested end-to-end without a real canvas portal.

Tests cover:
  - CanvasGestureSpec schema roundtrip on SkillStep.canvas_gesture.
  - pilot.adapters registers the noop_click adapter at import time.
  - register_canvas_adapter + get_canvas_adapter contract.
  - SkillRunner._do_canvas_gesture returns action_not_implemented for
    unknown adapter names and dispatches through the registered
    adapter when found.
"""

from __future__ import annotations

from pathlib import Path
import tempfile

from pilot.adapters import (
    CanvasAdapter,
    CanvasNoopClickAdapter,
    get_canvas_adapter,
    register_canvas_adapter,
)
from pilot.browser import BrowserSession
from pilot.models import ToolResult
from pilot.skill_models import (
    CanvasGestureSpec,
    ElementFingerprint,
    Skill,
    SkillStep,
)


# ----- schema --------------------------------------------------------------


def test_canvas_gesture_spec_roundtrip() -> None:
    spec = CanvasGestureSpec(
        adapter_name="noop_click",
        target_descriptor={"selector": "[data-testid='chart-canvas']"},
        action_payload={"kind": "click_center"},
        expected_postcondition="visible",
    )
    step = SkillStep(
        index=0,
        action="canvas_gesture",
        fingerprint=ElementFingerprint(test_id="chart-canvas"),
        canvas_gesture=spec,
    )
    data = step.model_dump()
    restored = SkillStep.model_validate(data)
    assert restored.action == "canvas_gesture"
    assert restored.canvas_gesture is not None
    assert restored.canvas_gesture.adapter_name == "noop_click"
    assert restored.canvas_gesture.target_descriptor == {
        "selector": "[data-testid='chart-canvas']"
    }
    assert restored.canvas_gesture.action_payload == {"kind": "click_center"}


# ----- adapter registry ----------------------------------------------------


def test_noop_click_adapter_registered_at_import() -> None:
    cls = get_canvas_adapter("noop_click")
    assert cls is CanvasNoopClickAdapter
    assert cls is not None
    assert cls.name == "noop_click"


def test_get_canvas_adapter_returns_none_for_unknown() -> None:
    assert get_canvas_adapter("definitely-not-a-real-adapter") is None


def test_register_canvas_adapter_round_trip() -> None:
    class DummyAdapter(CanvasAdapter):
        name = "dummy_wi49_test"

        def dispatch(self, target_descriptor, action_payload):
            return ToolResult(success=True, action_taken="dummy")

    register_canvas_adapter("dummy_wi49_test", DummyAdapter)
    assert get_canvas_adapter("dummy_wi49_test") is DummyAdapter


# ----- runner dispatch -----------------------------------------------------


def _build_runner(skill: Skill, tmp: Path):
    from pilot.skill_runner import SkillRunner
    session = BrowserSession(
        playwright=None,  # type: ignore[arg-type]
        browser=None,  # type: ignore[arg-type]
        context=None,  # type: ignore[arg-type]
        page=object(),  # type: ignore[arg-type]
    )
    return SkillRunner(
        session=session,
        skill=skill,
        params={},
        sessions_dir=tmp,
    )


def test_do_canvas_gesture_returns_not_implemented_for_unknown_adapter() -> None:
    step = SkillStep(
        index=0,
        action="canvas_gesture",
        canvas_gesture=CanvasGestureSpec(
            adapter_name="this_adapter_is_not_registered",
        ),
    )
    skill = Skill(name="cg", steps=[step])
    with tempfile.TemporaryDirectory() as td:
        runner = _build_runner(skill, Path(td))
        result, level = runner._do_canvas_gesture(step)
    assert result.success is False
    assert result.error_kind == "action_not_implemented"
    assert result.error_details is not None
    assert (
        result.error_details.get("adapter_name")
        == "this_adapter_is_not_registered"
    )


def test_do_canvas_gesture_fails_without_spec() -> None:
    step = SkillStep(
        index=0,
        action="canvas_gesture",
        # spec is missing -> bad_step
    )
    skill = Skill(name="cg", steps=[step])
    with tempfile.TemporaryDirectory() as td:
        runner = _build_runner(skill, Path(td))
        result, level = runner._do_canvas_gesture(step)
    assert result.success is False
    assert result.error_kind == "bad_step"


def test_do_canvas_gesture_dispatches_through_registered_adapter() -> None:
    """The runner instantiates the registered adapter with (page, audit)
    and calls dispatch(target_descriptor, action_payload). We register
    a recording adapter so we can assert the runner actually handed
    the structured payload through."""
    captured: dict = {}

    class RecordingAdapter(CanvasAdapter):
        name = "wi49_recording_test_adapter"

        def dispatch(self, target_descriptor, action_payload):
            captured["td"] = dict(target_descriptor)
            captured["ap"] = dict(action_payload)
            return ToolResult(
                success=True,
                action_taken="recording",
                output={"captured": True},
            )

    register_canvas_adapter(
        "wi49_recording_test_adapter", RecordingAdapter
    )
    step = SkillStep(
        index=0,
        action="canvas_gesture",
        canvas_gesture=CanvasGestureSpec(
            adapter_name="wi49_recording_test_adapter",
            target_descriptor={"selector": "[data-testid='c']"},
            action_payload={"kind": "draw_box", "coords": [1, 2, 3, 4]},
        ),
    )
    skill = Skill(name="cg", steps=[step])
    with tempfile.TemporaryDirectory() as td:
        runner = _build_runner(skill, Path(td))
        result, level = runner._do_canvas_gesture(step)
    assert result.success is True
    assert captured["td"] == {"selector": "[data-testid='c']"}
    assert captured["ap"] == {"kind": "draw_box", "coords": [1, 2, 3, 4]}
