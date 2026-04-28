"""Clarify stage.

The planner can already emit clarify questions when it cannot build a
plan. This module adds two pieces:

  1. A hard budget on rounds. Once the operator has answered N rounds
     of clarify questions and the planner still wants more, we stop and
     fail the task with `clarify_budget_exhausted` rather than asking
     forever.
  2. A helper to fold operator answers back into the planner input as
     additional context (currently appended to the goal as a structured
     section the planner reads).

This module is intentionally small. The "intelligence" of generating
questions lives in the planner; this module is plumbing for the
question-answer loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pilot.agent.schemas.protocol import ClarifyQuestion


DEFAULT_MAX_ROUNDS = 3


@dataclass
class ClarifyAnswer:
    question_id: str
    question_text: str
    answer_value: str
    answer_label: str | None = None


@dataclass
class ClarifyState:
    """Per-task state for the clarify loop."""

    max_rounds: int = DEFAULT_MAX_ROUNDS
    rounds_used: int = 0
    history: list[ClarifyAnswer] = field(default_factory=list)
    pending: list[ClarifyQuestion] = field(default_factory=list)

    def can_ask_more(self) -> bool:
        return self.rounds_used < self.max_rounds

    def record_answers(self, answers: list[ClarifyAnswer]) -> None:
        self.history.extend(answers)
        self.rounds_used += 1
        self.pending = []

    def to_goal_addendum(self) -> str:
        """Render the answer history as a block to append to the goal.

        The planner sees this on the next call and can use it to
        disambiguate without repeating questions.
        """
        if not self.history:
            return ""
        lines = ["", "Operator's clarification answers (do not re-ask these):"]
        for ans in self.history:
            label = ans.answer_label or ans.answer_value
            lines.append(f"- Q: {ans.question_text}")
            lines.append(f"  A: {label}")
        return "\n".join(lines)
