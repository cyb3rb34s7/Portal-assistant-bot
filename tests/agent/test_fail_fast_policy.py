"""WI-07: replay_policy.on_failure default abort + continue/optional opt-outs.

Pins the behavior that the runner stops the skill on a failed
non-optional step by default. Orchestrator pause / retry / skip
remains the recovery surface; the runner just doesn't keep mutating
on its own.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

from pilot.models import ToolResult
from pilot.skill_models import (
    ElementFingerprint,
    ReplayPolicy,
    Skill,
    SkillStep,
)
from pilot.skill_runner import SkillRunner


def _make_skill(*step_dicts) -> Skill:
    return Skill(
        name="t",
        steps=[SkillStep(**d) for d in step_dicts],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


class _StubRunner(SkillRunner):
    """SkillRunner subclass that lets us inject canned per-step results
    without driving a real browser. Overrides _execute_step to consume
    a queue of (result, level) pairs the test provides."""

    def __init__(self, skill: Skill, results: list[tuple[ToolResult, int]]):
        super().__init__(
            session=None,  # type: ignore[arg-type]
            skill=skill,
            params={},
            sessions_dir=Path(tempfile.gettempdir()),
        )
        self._injected = list(results)
        self.executed_step_indexes: list[int] = []

    def _execute_step(self, step):
        self.executed_step_indexes.append(step.index)
        # Pop from the front so the order matches step order.
        if not self._injected:
            return (
                ToolResult(success=True, action_taken="no canned result"),
                1,
            )
        return self._injected.pop(0)


def test_runner_aborts_on_default_policy() -> None:
    """A step with default ReplayPolicy (on_failure='abort') stops the
    runner immediately when it fails."""
    skill = _make_skill(
        {"index": 0, "action": "click"},
        {"index": 1, "action": "click"},  # this should NOT execute
        {"index": 2, "action": "click"},
    )
    runner = _StubRunner(
        skill,
        results=[
            (ToolResult(success=False, action_taken="step 0 boom"), 0),
        ],
    )
    runner.run()
    assert runner.executed_step_indexes == [0], (
        f"runner should have stopped after step 0; "
        f"actually ran {runner.executed_step_indexes}"
    )


def test_runner_continues_when_policy_says_continue() -> None:
    """A step that explicitly opts into continue lets the runner
    proceed to the next step."""
    step0 = SkillStep(
        index=0,
        action="click",
        replay_policy=ReplayPolicy(on_failure="continue"),
    )
    step1 = SkillStep(index=1, action="click")
    skill = Skill(
        name="t", steps=[step0, step1],
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    runner = _StubRunner(
        skill,
        results=[
            (ToolResult(success=False, action_taken="step 0 boom"), 0),
            (ToolResult(success=True, action_taken="step 1 ok"), 1),
        ],
    )
    runner.run()
    assert runner.executed_step_indexes == [0, 1]


def test_optional_flag_equivalent_to_continue() -> None:
    """``replay_policy.optional=True`` is shorthand for
    on_failure='optional'. Either way, the runner proceeds."""
    step0 = SkillStep(
        index=0,
        action="click",
        replay_policy=ReplayPolicy(optional=True),
    )
    step1 = SkillStep(index=1, action="click")
    skill = Skill(
        name="t", steps=[step0, step1],
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    runner = _StubRunner(
        skill,
        results=[
            (ToolResult(success=False, action_taken="step 0 boom"), 0),
            (ToolResult(success=True, action_taken="step 1 ok"), 1),
        ],
    )
    runner.run()
    assert runner.executed_step_indexes == [0, 1]
