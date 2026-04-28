"""Hand-written agent orchestrator.

Linear state machine: intake -> (clarify-loop) -> plan -> approve ->
execute -> report. Emits typed events at every transition. Designed
to be driven by either:

  - the JSON-RPC server (`pilot.agent.server`), which streams events
    over stdio; or
  - the CLI driver (`pilot.cli.agent`), which renders events to the
    terminal.

Concretely:

  ev_queue: asyncio.Queue[AgentEvent]      # agent -> host
  cmd_queue: asyncio.Queue[HostCommand]    # host -> agent

Both queues are owned by the caller. The orchestrator is a free-running
async task that reads the cmd queue when it's blocked on a host
response (clarify, plan approval, pause resolution) and writes the ev
queue continuously.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pilot.agent.ai_client import AIClient
from pilot.agent.clarify import ClarifyAnswer, ClarifyState
from pilot.agent.intake import run_intake
from pilot.agent.planner import PlannerOutput, load_skill_library, run_planner
from pilot.agent.reporter import write_report
from pilot.agent.schemas.domain import (
    DestructiveAction,
    Plan,
    PlanStep,
)
from pilot.agent.schemas.portal_context import PortalContext
from pilot.agent.schemas.protocol import (
    AgentEvent,
    AgentLog,
    ClarifyAnswer as ClarifyAnswerCmd,
    ClarifyAsk,
    ClarifyOption,
    HostCommand,
    IntakeExtracted,
    PauseResolve,
    Paused,
    PlanApprove,
    PlanProposed,
    PlanReject,
    ReportReady,
    StepFailedEvent,
    StepHealedEvent,
    StepProgressEvent,
    StepStartedEvent,
    StepSucceededEvent,
    TaskCancel,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
    TaskSubmit,
)
from pilot.agent.schemas.skill import SkillFile


# ---------------------------------------------------------------------------
# Pluggable executor
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    succeeded: bool
    duration_ms: int = 0
    error_kind: str | None = None
    error_message: str | None = None
    screenshot_path: str | None = None
    heals: list[dict[str, Any]] = field(default_factory=list)
    """One entry per sub-step inside this plan step that the runner
    self-healed (L3). Each dict carries ``original_summary``,
    ``new_summary``, ``confidence``, ``reason``,
    ``post_condition_passed``, ``persisted_to_skill``. The orchestrator
    emits a ``StepHealedEvent`` per entry between progress and
    succeeded (or before failed)."""


class StepExecutor:
    """Abstract executor interface.

    The real executor calls into `pilot.skill_runner` to drive the
    portal via CDP. The fake executor in `FakeExecutor` (below) is for
    smoke tests and CLI dry-runs without a live browser.
    """

    async def execute(
        self,
        step: PlanStep,
        skill: SkillFile,
        emit_progress: "callable[[str, dict[str, Any]], None]",
    ) -> StepResult:
        raise NotImplementedError


class FakeExecutor(StepExecutor):
    """Pretends to run each step. Used for v1 smoke tests until the
    skill_runner integration lands in week 2."""

    def __init__(self, *, fail_step_idxs: set[int] | None = None) -> None:
        self.fail_step_idxs = fail_step_idxs or set()

    async def execute(
        self,
        step: PlanStep,
        skill: SkillFile,
        emit_progress,
    ) -> StepResult:
        start = time.time()
        # Simulate a few sub-actions per step
        await asyncio.sleep(0.05)
        emit_progress("click", {"test_id": f"start-{step.skill_id}"})
        await asyncio.sleep(0.05)
        emit_progress("fill", {"test_id": "input-asset-id"})
        await asyncio.sleep(0.05)
        emit_progress("click", {"test_id": "btn-save"})

        if step.idx in self.fail_step_idxs:
            return StepResult(
                succeeded=False,
                duration_ms=int((time.time() - start) * 1000),
                error_kind="locator_exhausted",
                error_message=f"(fake) step {step.idx} configured to fail",
            )
        return StepResult(succeeded=True, duration_ms=int((time.time() - start) * 1000))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    sessions_dir: Path
    skills_dir: Path
    portal_context: PortalContext | None = None
    intake_use_llm: bool = True
    auto_approve_plan: bool = False
    """If True, the operator approval step is bypassed. Used by the CLI's
    --auto-approve flag and by integration tests; never set this in
    production UI flows."""

    intake_model: str | None = None
    planner_model: str | None = None
    reporter_model: str | None = None
    """Per-stage model overrides. None means each stage uses the
    AIClient's default_model."""


class Orchestrator:
    def __init__(
        self,
        *,
        client: AIClient,
        config: OrchestratorConfig,
        ev_out: asyncio.Queue[AgentEvent],
        cmd_in: asyncio.Queue[HostCommand],
        executor: StepExecutor | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.ev_out = ev_out
        self.cmd_in = cmd_in
        self.executor = executor or FakeExecutor()

        self.task_id: str | None = None
        self.session_id: str | None = None
        self.session_dir: Path | None = None
        self._cancelled = False
        self._step_records: list[dict[str, Any]] = []

    # ---- Event helpers ---------------------------------------------------

    async def _emit(self, event: AgentEvent) -> None:
        if hasattr(event, "stamp"):
            event.stamp()  # type: ignore[attr-defined]
        await self.ev_out.put(event)

    async def _log(self, message: str, *, level: str = "info", **ctx: Any) -> None:
        await self._emit(AgentLog(level=level, message=message, context=ctx))  # type: ignore[arg-type]

    # ---- Command intake (for clarify / approve / pause) ----------------

    async def _next_command_for_task(self) -> HostCommand:
        """Wait for the next host command targeted at this task.

        Drops commands targeting other task ids (a task.cancel for a
        different id is logged but not actioned). Returns when a command
        for the current task arrives.
        """
        assert self.task_id is not None
        while True:
            cmd = await self.cmd_in.get()
            cmd_task_id = getattr(cmd, "task_id", None)
            if cmd_task_id != self.task_id:
                await self._log(
                    f"ignored host command for other task: {cmd_task_id}",
                    level="debug",
                )
                continue
            if isinstance(cmd, TaskCancel):
                self._cancelled = True
            return cmd

    # ---- Skill library + portal context loading ------------------------

    async def _load_skills(self) -> list[SkillFile]:
        errors: list[str] = []
        skills = load_skill_library(self.config.skills_dir, errors=errors)
        for msg in errors:
            await self._log(msg, level="warn", source="skill_loader")
        return skills

    # ---- Run a task ----------------------------------------------------

    async def run_task(self, submit: TaskSubmit) -> None:
        """Top-level entry: drive a single task to completion."""
        self.task_id = submit.task_id
        self.session_id = uuid.uuid4().hex[:12]
        self.session_dir = self.config.sessions_dir / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._cancelled = False
        self._step_records = []

        try:
            await self._run_task_inner(submit)
        finally:
            # Per-task cleanup — close any browser/CDP resources held by
            # the executor. Safe even if the executor was a FakeExecutor
            # without a close method, or if the task was cancelled mid-run.
            close = getattr(self.executor, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:  # noqa: BLE001
                    await self._log(
                        f"executor.close raised: {type(e).__name__}: {e}",
                        level="warn",
                    )

    async def _run_task_inner(self, submit: TaskSubmit) -> None:
        try:
            # ---- Intake ----
            entities = await run_intake(
                client=self.client,
                goal=submit.goal,
                attachments=submit.attachments,
                use_llm=self.config.intake_use_llm,
                model=self.config.intake_model,
            )
            await self._emit(
                IntakeExtracted(
                    task_id=self.task_id,
                    entities=entities,
                    intake_warnings=entities.warnings,
                )
            )
            if self._cancelled:
                return await self._cancel_task()

            # ---- Plan + clarify loop ----
            skills = await self._load_skills()
            clarify_state = ClarifyState()
            goal_text = submit.goal

            while True:
                planner_out = await run_planner(
                    client=self.client,
                    goal=goal_text + clarify_state.to_goal_addendum(),
                    entities=entities,
                    skills=skills,
                    portal=self.config.portal_context,
                    model=self.config.planner_model,
                )

                if planner_out.plan is not None:
                    plan = planner_out.plan
                    break

                # No plan; need clarify. Budget check.
                if not clarify_state.can_ask_more():
                    await self._emit(
                        TaskFailed(
                            task_id=self.task_id,
                            error_kind="clarify_budget_exhausted",
                            error_message=(
                                f"Asked {clarify_state.rounds_used} clarify "
                                "rounds without converging. Aborting."
                            ),
                        )
                    )
                    return

                # Ask each pending question one at a time. (Most planners
                # emit one or two; we sequence for clean UX.)
                answers_this_round: list[ClarifyAnswer] = []
                for q in planner_out.clarify_questions:
                    await self._emit(
                        ClarifyAsk(
                            task_id=self.task_id,
                            id=q.id,
                            question=q.question,
                            options=q.options,
                            allow_custom_answer=q.allow_custom_answer,
                            priority=q.priority,
                        )
                    )
                    cmd = await self._next_command_for_task()
                    if isinstance(cmd, TaskCancel):
                        return await self._cancel_task()
                    if isinstance(cmd, ClarifyAnswerCmd):
                        answers_this_round.append(
                            ClarifyAnswer(
                                question_id=cmd.question_id,
                                question_text=q.question,
                                answer_value=cmd.answer_value,
                                answer_label=cmd.answer_label,
                            )
                        )
                    else:
                        await self._log(
                            f"unexpected cmd during clarify: {cmd.type}",
                            level="warn",
                        )
                clarify_state.record_answers(answers_this_round)

            # ---- Plan approval ----
            await self._emit(
                PlanProposed(
                    task_id=self.task_id,
                    id=plan.id,
                    summary=plan.summary,
                    skill_summary=plan.skill_summary,
                    steps=plan.steps,
                    destructive_actions=plan.destructive_actions,
                    estimated_duration_seconds=plan.estimated_duration_seconds,
                    preconditions=plan.preconditions,
                )
            )

            if not self.config.auto_approve_plan:
                cmd = await self._next_command_for_task()
                if isinstance(cmd, TaskCancel):
                    return await self._cancel_task()
                if isinstance(cmd, PlanReject):
                    await self._emit(
                        TaskFailed(
                            task_id=self.task_id,
                            error_kind="plan_rejected",
                            error_message=cmd.reason or "operator rejected the plan",
                        )
                    )
                    return
                if not isinstance(cmd, PlanApprove):
                    await self._log(
                        f"unexpected cmd during plan approval: {cmd.type}",
                        level="warn",
                    )

            # ---- Execute ----
            skills_by_id = {s.id: s for s in skills}
            loop = asyncio.get_running_loop()

            def _make_progress_emitter(step_idx: int):
                """Build a sync emit_progress callback bound to step_idx.

                The factory captures step_idx by value so executor
                callbacks fired after the step loop has advanced still
                attribute progress to the correct step.
                """
                def _emit_progress_sync(
                    action: str, info: dict[str, Any]
                ) -> None:
                    coro = self._emit(
                        StepProgressEvent(
                            task_id=self.task_id,  # type: ignore[arg-type]
                            idx=step_idx,
                            action=action,
                            test_id=info.get("test_id"),
                            screenshot_path=info.get("screenshot_path"),
                        )
                    )
                    try:
                        asyncio.run_coroutine_threadsafe(coro, loop)
                    except RuntimeError:
                        # Loop closed (task cancellation race); drop.
                        pass

                return _emit_progress_sync

            for step in plan.steps:
                if self._cancelled:
                    return await self._cancel_task()

                skill = skills_by_id.get(step.skill_id)
                if skill is None:
                    await self._record_step(step, status="skipped", duration_ms=0)
                    await self._emit(
                        StepFailedEvent(
                            task_id=self.task_id,
                            idx=step.idx,
                            error_kind="missing_skill",
                            error_message=f"skill {step.skill_id} not found",
                        )
                    )
                    continue

                await self._emit(
                    StepStartedEvent(
                        task_id=self.task_id,
                        idx=step.idx,
                        skill_id=step.skill_id,
                        params=step.params,
                    )
                )

                emit_progress = _make_progress_emitter(step.idx)
                result = await self.executor.execute(step, skill, emit_progress)

                # Emit one StepHealed per heal that happened during this
                # plan step, BEFORE succeeded/failed so the UI can show
                # the heal banner alongside the step.
                for h in result.heals:
                    await self._emit(
                        StepHealedEvent(
                            task_id=self.task_id,
                            idx=step.idx,
                            original_summary=str(h.get("original_summary", "?")),
                            new_summary=str(h.get("new_summary", "?")),
                            confidence=h.get("confidence", "low"),
                            reason=str(h.get("reason", "")),
                            post_condition_passed=bool(
                                h.get("post_condition_passed", False)
                            ),
                            persisted_to_skill=bool(
                                h.get("persisted_to_skill", False)
                            ),
                        )
                    )

                if result.succeeded:
                    await self._record_step(
                        step, status="succeeded", duration_ms=result.duration_ms
                    )
                    await self._emit(
                        StepSucceededEvent(
                            task_id=self.task_id,
                            idx=step.idx,
                            duration_ms=result.duration_ms,
                        )
                    )
                else:
                    await self._handle_step_failure(
                        step, skill, result, skills_by_id, emit_progress
                    )
                    if self._cancelled:
                        return await self._cancel_task()

            # ---- Report ----
            report_path = await self._build_report(plan)
            await self._emit(
                ReportReady(
                    task_id=self.task_id,
                    session_id=self.session_id,
                    report_path=str(report_path),
                    summary=self._compose_summary(plan),
                    warnings=[],
                )
            )
            await self._emit(
                TaskCompleted(task_id=self.task_id, session_id=self.session_id)
            )
        except asyncio.CancelledError:
            await self._cancel_task()
            raise
        except Exception as e:  # noqa: BLE001 - surfaced as task.failed
            await self._emit(
                TaskFailed(
                    task_id=self.task_id,
                    error_kind="agent_internal_error",
                    error_message=f"{type(e).__name__}: {e}",
                )
            )

    # ---- Failure handling ----------------------------------------------

    async def _handle_step_failure(
        self,
        step: PlanStep,
        skill: SkillFile,
        result: StepResult,
        skills_by_id: dict[str, SkillFile],
        emit_progress: Callable[[str, dict[str, Any]], None],
    ) -> None:
        await self._emit(
            StepFailedEvent(
                task_id=self.task_id,  # type: ignore[arg-type]
                idx=step.idx,
                error_kind=result.error_kind or "step_failed",
                error_message=result.error_message or "step failed",
                screenshot_path=result.screenshot_path,
                suggestions=[
                    {"action": "retry", "label": "Retry this step"},
                    {"action": "skip", "label": "Skip and continue"},
                    {"action": "abort", "label": "Abort the run"},
                ],
            )
        )

        pause_id = f"pause-{uuid.uuid4().hex[:6]}"
        await self._emit(
            Paused(
                task_id=self.task_id,  # type: ignore[arg-type]
                pause_id=pause_id,
                reason="step_failed",
                context={"step_idx": step.idx},
            )
        )

        cmd = await self._next_command_for_task()
        if isinstance(cmd, TaskCancel):
            return
        if not isinstance(cmd, PauseResolve):
            await self._log(
                f"unexpected cmd during pause: {cmd.type}", level="warn"
            )
            await self._record_step(step, status="failed", duration_ms=result.duration_ms)
            return

        if cmd.action == "abort":
            await self._record_step(step, status="failed", duration_ms=result.duration_ms)
            await self._emit(
                TaskFailed(
                    task_id=self.task_id,  # type: ignore[arg-type]
                    error_kind=result.error_kind or "step_failed",
                    error_message=result.error_message or "aborted by operator",
                )
            )
            self._cancelled = True
            return
        if cmd.action == "skip":
            await self._record_step(step, status="skipped", duration_ms=0)
            return
        # retry / use_alternate (use_alternate is treated as retry in v1)
        retry = await self.executor.execute(step, skill, emit_progress)
        status = "succeeded" if retry.succeeded else "failed"
        await self._record_step(step, status=status, duration_ms=retry.duration_ms)
        if retry.succeeded:
            await self._emit(
                StepSucceededEvent(
                    task_id=self.task_id,  # type: ignore[arg-type]
                    idx=step.idx,
                    duration_ms=retry.duration_ms,
                )
            )
        else:
            await self._emit(
                StepFailedEvent(
                    task_id=self.task_id,  # type: ignore[arg-type]
                    idx=step.idx,
                    error_kind=retry.error_kind or "step_failed",
                    error_message=retry.error_message or "retry failed",
                )
            )

    async def _record_step(self, step: PlanStep, *, status: str, duration_ms: int) -> None:
        self._step_records.append(
            {
                "idx": step.idx,
                "skill_id": step.skill_id,
                "params": step.params,
                "status": status,
                "duration_ms": duration_ms,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

    # ---- Report building -----------------------------------------------

    def _compose_summary(self, plan: Plan) -> str:
        ok = sum(1 for s in self._step_records if s["status"] == "succeeded")
        total = len(plan.steps)
        return f"{ok}/{total} steps succeeded."

    async def _build_report(self, plan: Plan) -> Path:
        assert self.session_dir is not None
        return await write_report(
            session_dir=self.session_dir,
            session_id=self.session_id or "?",
            summary=self._compose_summary(plan),
            steps=self._step_records,
            warnings=[],
            client=self.client,
            model=self.config.reporter_model,
        )

    async def _cancel_task(self) -> None:
        await self._emit(TaskCancelled(task_id=self.task_id or "?"))
