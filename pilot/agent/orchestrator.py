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
from typing import Any, Callable, Literal

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
class PreflightResult:
    """Outcome of the pre-flight phase that runs before intake.

    The orchestrator uses this to decide whether to proceed, ask the
    operator to launch a portal browser, or pause for authentication.
    """

    cdp_reachable: bool
    """Could we connect to Chrome's DevTools Protocol?"""
    matching_tab_url: str | None = None
    """URL of the open tab that matches the portal's base_url, if any."""
    auth_status: Literal["ok", "missing", "unknown"] = "unknown"
    """``ok``: positive signal found OR negative signal not found.
    ``missing``: the negative signal (login form, etc.) was present.
    ``unknown``: portal context didn't declare an auth_signal."""
    diagnostic: str = ""
    """Operator-readable summary for the agent.log event."""


@dataclass
class StepResult:
    succeeded: bool
    duration_ms: int = 0
    error_kind: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    """Structured payload keyed by error_kind. For ambiguous_target carries
    {candidates: [...], verb: ...} so the UI can render a row picker
    instead of a generic retry/skip/abort."""
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

    async def preflight(
        self,
        base_url: str | None,
        auth_signal: Any | None = None,
    ) -> PreflightResult:
        """Pre-flight probe before intake runs.

        Default impl returns a "looks fine" result so executors that
        don't drive a real browser (FakeExecutor) don't block the task.
        Real executors override to actually probe CDP + tab + auth.
        """
        _ = base_url, auth_signal
        return PreflightResult(
            cdp_reachable=True,
            matching_tab_url=base_url,
            auth_status="unknown",
            diagnostic="preflight skipped (default executor)",
        )

    async def execute(
        self,
        step: PlanStep,
        skill: SkillFile,
        emit_progress: "callable[[str, dict[str, Any]], None]",
        sub_step_overrides: dict[int, dict[str, Any]] | None = None,
    ) -> StepResult:
        """Run a plan step.

        ``sub_step_overrides`` lets the orchestrator force a specific
        sub-step inside the skill to target a specific element instead
        of resolving via its templated fingerprint. Used when the
        operator resolves an ``ambiguous_target`` failure by picking a
        candidate -- the orchestrator forwards
        ``{sub_step_index: {test_id: <picked>}}`` to the executor,
        which threads it into the runner so the next attempt clicks
        the right one. Empty/None means no override (default behavior).
        """
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
        sub_step_overrides: dict[int, dict[str, Any]] | None = None,
    ) -> StepResult:
        _ = sub_step_overrides  # FakeExecutor doesn't honor overrides
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
    portals_dir: Path = Path("portals")
    """Where per-portal catalogs live. Loaded as
    ``portals/<portal_id>/catalog.yaml`` if present."""
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

    async def _audit_external_llm(
        self, stage: str, model: str | None = None
    ) -> None:
        """Emit an audit-trail log entry for every cloud-LLM call.

        Lightweight visibility -- not redaction. Future enterprise mode
        will gate this by allow_external_llm and run a redaction pass
        on the payload before send. Today's job is just to make every
        external call discoverable in the session log so an operator
        (or auditor) can count them.
        """
        portal = self.config.portal_context
        await self._log(
            f"external LLM call: stage={stage} model={model or 'default'}",
            level="info",
            source="external_llm_call",
            stage=stage,
            model=model or self.client.default_model,
            client=self.client.name,
            portal_id=portal.portal_id if portal else None,
        )

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

    def _load_catalog_block(self, portal_id: str | None) -> str:
        """Build the rendered-for-prompt catalog string for the portal,
        if available. Empty string if no portal_id or no catalog file.

        The catalog augments the planner with passively-observed portal
        knowledge — pages, dropdown options, form fields — that the
        portal_context.yaml might not have hand-authored. Used purely
        as additional grounding text in the planner prompt.
        """
        if not portal_id:
            return ""
        try:
            from pilot.agent.catalog import (
                catalog_path,
                load_catalog,
                render_for_prompt as render_catalog,
            )
        except Exception:
            return ""
        if not catalog_path(self.config.portals_dir, portal_id).exists():
            return ""
        try:
            cat = load_catalog(self.config.portals_dir, portal_id)
            return render_catalog(cat)
        except Exception:
            return ""

    # ---- Pre-flight ---------------------------------------------------

    async def _preflight_phase(self, submit: TaskSubmit) -> bool:
        """Run executor.preflight; if auth is missing, pause and let
        the operator log in, then re-probe. Returns True if the task
        should proceed, False if it was cancelled / failed.

        Bounded by a single retry: if auth is *still* missing after the
        operator clicks Resume, we emit task.failed rather than looping.
        Operators who want to abort can use the modal's Abort button.
        """
        portal = self.config.portal_context
        base_url = portal.base_url if portal else None
        auth_signal = portal.auth_signal if portal else None

        for attempt in (1, 2):
            try:
                pf = await self.executor.preflight(base_url, auth_signal)
            except Exception as e:  # noqa: BLE001
                await self._emit(
                    TaskFailed(
                        task_id=self.task_id,  # type: ignore[arg-type]
                        error_kind="preflight_crashed",
                        error_message=f"{type(e).__name__}: {e}",
                    )
                )
                return False

            await self._log(
                f"preflight: {pf.diagnostic}",
                level="info",
                source="preflight",
                cdp_reachable=pf.cdp_reachable,
                matching_tab_url=pf.matching_tab_url,
                auth_status=pf.auth_status,
            )

            if not pf.cdp_reachable:
                await self._emit(
                    TaskFailed(
                        task_id=self.task_id,  # type: ignore[arg-type]
                        error_kind="cdp_unreachable",
                        error_message=(
                            "Chrome with --remote-debugging-port=9222 is not "
                            "running. Launch the portal browser from the UI's "
                            "Home tab and resubmit. " + pf.diagnostic
                        ),
                    )
                )
                return False

            if pf.auth_status != "missing":
                return True  # ok or unknown -> proceed

            # auth_status == "missing": pause and let operator log in.
            if attempt == 2:
                await self._emit(
                    TaskFailed(
                        task_id=self.task_id,  # type: ignore[arg-type]
                        error_kind="auth_required",
                        error_message=(
                            "Portal still appears unauthenticated after "
                            "Resume. Sign in to the portal tab and try again."
                        ),
                    )
                )
                return False

            pause_id = f"pause-auth-{uuid.uuid4().hex[:6]}"
            await self._emit(
                Paused(
                    task_id=self.task_id,  # type: ignore[arg-type]
                    pause_id=pause_id,
                    reason="auth_required",
                    context={
                        "diagnostic": pf.diagnostic,
                        "matching_tab_url": pf.matching_tab_url,
                    },
                )
            )
            cmd = await self._next_command_for_task()
            if isinstance(cmd, TaskCancel):
                await self._cancel_task()
                return False
            if not isinstance(cmd, PauseResolve):
                await self._log(
                    f"unexpected cmd during auth pause: {cmd.type}", level="warn"
                )
                continue  # try again; conservative
            if cmd.action == "abort":
                await self._emit(
                    TaskFailed(
                        task_id=self.task_id,  # type: ignore[arg-type]
                        error_kind="auth_required",
                        error_message="operator aborted at auth pause",
                    )
                )
                return False
            # retry / skip / use_alternate -> re-probe on the next loop turn
        return False

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
            # ---- External-LLM kill switch ----
            # If the portal's context.yaml has external_llm_enabled: false,
            # the orchestrator can't call cloud models for intake/plan/
            # report. Fail fast with a clear message; we don't try to
            # silently fall back to deterministic-only mode because most
            # operators would expect the goal to still be parsed.
            portal = self.config.portal_context
            if portal is not None and not portal.external_llm_enabled:
                await self._emit(
                    TaskFailed(
                        task_id=self.task_id,  # type: ignore[arg-type]
                        error_kind="external_llm_disabled",
                        error_message=(
                            f"Portal {portal.portal_id} has "
                            "external_llm_enabled=false in context.yaml; "
                            "cloud LLM calls refused. Flip the toggle or "
                            "use a different portal."
                        ),
                    )
                )
                return

            # ---- Pre-flight ----
            # Probe CDP + tab + auth BEFORE we burn LLM tokens on
            # intake/planning. If Chrome isn't up, or the operator is
            # logged out, fail fast with an operator-actionable message
            # instead of crashing mid-step five minutes from now.
            if not await self._preflight_phase(submit):
                return

            # ---- Intake ----
            # Audit emitted AFTER the call returns so failures don't show
            # phantom external-LLM calls that never actually happened.
            entities = await run_intake(
                client=self.client,
                goal=submit.goal,
                attachments=submit.attachments,
                use_llm=self.config.intake_use_llm,
                model=self.config.intake_model,
                portal=self.config.portal_context,
            )
            if self.config.intake_use_llm:
                await self._audit_external_llm("intake", self.config.intake_model)
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
            catalog_block = self._load_catalog_block(submit.portal_id)

            while True:
                planner_out = await run_planner(
                    client=self.client,
                    goal=goal_text + clarify_state.to_goal_addendum(),
                    entities=entities,
                    skills=skills,
                    portal=self.config.portal_context,
                    model=self.config.planner_model,
                    catalog_block=catalog_block,
                )
                # Only audit when the planner actually called the LLM.
                # The empty-skills early return (planner.py) emits a
                # canned clarify question without invoking the model;
                # auditing it would produce a phantom external-LLM
                # entry in the session log.
                if getattr(planner_out, "notes", None) != "empty_skill_library":
                    await self._audit_external_llm(
                        "planner", self.config.planner_model
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

            # Safety: auto-approve never bypasses approval when the plan
            # contains an irreversible destructive action (publish, delete,
            # archive, etc. with reversible=False). Operator MUST eyeball
            # those even with --auto-approve set. Reversible saves are
            # still auto-approveable.
            irreversible_present = any(
                not da.reversible for da in plan.destructive_actions
            )
            effective_auto_approve = self.config.auto_approve_plan and not irreversible_present
            if self.config.auto_approve_plan and irreversible_present:
                await self._log(
                    "auto-approve requested but plan contains "
                    f"{sum(1 for da in plan.destructive_actions if not da.reversible)} "
                    "irreversible destructive action(s); requiring human approval",
                    level="warn",
                    source="auto_approve_safety",
                )

            if not effective_auto_approve:
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
            # Print the full traceback to stderr so the FastAPI terminal
            # actually shows what failed. Without this, the operator sees
            # only the short "APIConnectionError: Connection error." in
            # the UI and has to guess where it came from.
            import sys
            import traceback as _tb
            _tb.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
            sys.stderr.flush()
            # Also surface the traceback's last frame in the event so the
            # UI shows WHERE it failed, not just WHAT.
            tb_str = "".join(_tb.format_exception(type(e), e, e.__traceback__))
            await self._log(
                f"agent task crashed: {type(e).__name__}: {e}",
                level="error",
                source="orchestrator",
                traceback=tb_str,
            )
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
        # Tailor suggestions to the failure shape. For ambiguous_target
        # we add a use_alternate suggestion so the UI lights up the
        # row-picker mode instead of just retry/skip/abort.
        suggestions = [
            {"action": "retry", "label": "Retry this step"},
            {"action": "skip", "label": "Skip and continue"},
            {"action": "abort", "label": "Abort the run"},
        ]
        if result.error_kind == "ambiguous_target":
            suggestions = [
                {"action": "use_alternate", "label": "Pick which to use"},
                *suggestions,
            ]

        await self._emit(
            StepFailedEvent(
                task_id=self.task_id,  # type: ignore[arg-type]
                idx=step.idx,
                error_kind=result.error_kind or "step_failed",
                error_message=result.error_message or "step failed",
                screenshot_path=result.screenshot_path,
                suggestions=suggestions,
                error_details=result.error_details,
            )
        )

        pause_context: dict[str, Any] = {"step_idx": step.idx}
        if result.error_details:
            pause_context["error_details"] = result.error_details
            pause_context["error_kind"] = result.error_kind
        pause_id = f"pause-{uuid.uuid4().hex[:6]}"
        await self._emit(
            Paused(
                task_id=self.task_id,  # type: ignore[arg-type]
                pause_id=pause_id,
                reason="step_failed",
                context=pause_context,
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

        # use_alternate: operator picked a specific candidate from the
        # ambiguous_target row picker. Build a sub_step_override so the
        # next run targets that exact element instead of the templated
        # fingerprint that caused the ambiguity in the first place.
        overrides: dict[int, dict[str, Any]] | None = None
        if cmd.action == "use_alternate" and cmd.payload:
            candidate = cmd.payload.get("candidate") or {}
            sub_step_index = (
                cmd.payload.get("sub_step_index")
                or (result.error_details or {}).get("sub_step_index")
            )
            override_fields: dict[str, Any] = {}
            if candidate.get("test_id"):
                override_fields["test_id"] = candidate["test_id"]
            elif candidate.get("id"):
                override_fields["id"] = candidate["id"]
            if isinstance(sub_step_index, int) and override_fields:
                overrides = {sub_step_index: override_fields}
                await self._log(
                    f"using operator-picked alternate for sub-step {sub_step_index}",
                    level="info",
                    source="use_alternate",
                    override=override_fields,
                )

        retry = await self.executor.execute(
            step, skill, emit_progress, sub_step_overrides=overrides
        )
        status = "succeeded" if retry.succeeded else "failed"
        await self._record_step(step, status=status, duration_ms=retry.duration_ms)
        if retry.succeeded:
            # Learning loop: persist the operator's pick as a
            # disambiguation hint on the skill's sidecar so future runs
            # can auto-resolve the same ambiguity. Only fires on
            # use_alternate (when overrides is set) AND retry success;
            # other retries (plain re-execute) don't carry candidate
            # info to persist.
            if (
                overrides
                and cmd.action == "use_alternate"
                and cmd.payload
            ):
                await self._persist_disambiguation_hint(skill, cmd, result)
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

    async def _persist_disambiguation_hint(
        self,
        skill: SkillFile,
        cmd: Any,  # PauseResolve
        original_result: StepResult,
    ) -> None:
        """Write the operator's pick to the skill's hints sidecar.

        The chosen candidate + the rejected candidates from the original
        failure become the persistent record. Best-effort -- if the
        sidecar write fails, we just log it; the immediate retry already
        succeeded, so the operator's experience is unaffected.
        """
        payload = cmd.payload or {}
        candidate = payload.get("candidate") or {}
        sub_step_index = (
            payload.get("sub_step_index")
            or (original_result.error_details or {}).get("sub_step_index")
        )
        if not isinstance(sub_step_index, int):
            return
        # Without a candidate index there's no reliable way to compute
        # negative candidates -- skip persistence instead of guessing.
        # The runtime hint scoring still works on test_id/text alone,
        # so a future operator-resolution that DOES carry an index
        # will overwrite this skip.
        chosen_index = candidate.get("index")
        if chosen_index is None:
            return
        all_candidates = (
            (original_result.error_details or {}).get("candidates") or []
        )
        negative = [
            c
            for c in all_candidates
            if c.get("index") != chosen_index
        ]
        hint = {
            "chosen_text": candidate.get("text"),
            "chosen_test_id": candidate.get("test_id"),
            "chosen_neighbors": [],  # populated by future enrichment step
            "chosen_section": None,
            "negative_candidates": negative,
        }
        persist = getattr(self.executor, "persist_disambiguation_hint", None)
        if not callable(persist):
            return
        try:
            ok = persist(skill.id, sub_step_index, hint)
        except Exception:
            ok = False
        await self._log(
            (
                "persisted disambiguation hint"
                if ok
                else "failed to persist disambiguation hint (continuing)"
            ),
            level="info" if ok else "warn",
            source="disambiguation_hint",
            skill_id=skill.id,
            sub_step_index=sub_step_index,
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
        result = await write_report(
            session_dir=self.session_dir,
            session_id=self.session_id or "?",
            summary=self._compose_summary(plan),
            steps=self._step_records,
            warnings=[],
            client=self.client,
            model=self.config.reporter_model,
        )
        # Audit AFTER the call returns so a reporter exception
        # doesn't show up as a "called the LLM but it didn't" entry.
        await self._audit_external_llm("reporter", self.config.reporter_model)
        return result

    async def _cancel_task(self) -> None:
        await self._emit(TaskCancelled(task_id=self.task_id or "?"))
