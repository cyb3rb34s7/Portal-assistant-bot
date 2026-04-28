"""Real `StepExecutor` that drives the deterministic `pilot.skill_runner`.

One orchestrator-level step in our agent corresponds to **one full skill
invocation** (planner says: "use skill `curate_layout` with these
params"). We load the v1 Skill JSON from disk, connect to Chrome over
CDP, and let `SkillRunner` replay the recorded trace using the
4-level locator fallback.

Process model
-------------

The orchestrator is async; `SkillRunner` is sync (built on
`sync_playwright`). We bridge by running the runner in a worker thread
via `asyncio.to_thread()`. Per-step `emit_progress` callbacks are
batched into the runner's audit log; we stream them back to the
orchestrator as a final aggregate before returning.

The `SkillRunner` instance owns its `BrowserSession` for the duration
of the skill. We do **not** keep a session open across plan steps in
v1 — each plan step re-attaches to CDP. Cheap (≈100 ms) and avoids
state-leaking corner cases.

Why not feed individual `step.progress` events while the runner runs?
Because `SkillRunner.run()` is a synchronous tight loop. Streaming
mid-run would require either a callback-injection refactor of the
runner (out of scope for Phase 2) or a thread-safe queue between the
worker thread and the asyncio loop. The latter is cheap to add in
Phase 3 if real-time UI feedback is needed; for now the orchestrator
emits one `step.progress` at the end summarizing what the runner did.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pilot.audit import AuditLogger  # noqa: F401  (imported for type clarity)
from pilot.browser import (
    BrowserSession,
    DEFAULT_CDP_ENDPOINT,
    connect_to_chrome,
)
from pilot.skill_models import Skill
from pilot.skill_runner import SkillRunner

from pilot.agent.orchestrator import StepExecutor, StepResult
from pilot.agent.schemas.domain import PlanStep
from pilot.agent.schemas.skill import SkillFile


@dataclass
class RealExecutorConfig:
    skills_dir: Path
    sessions_dir: Path
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT
    target_url_substring: str | None = None
    """If set, the runner attaches to the page whose URL contains this
    substring. Useful when Chrome has multiple tabs open. Defaults to
    None (first non-devtools page)."""

    auto_approve_gates: bool = True
    """If True, all `requires_gate` steps in the recorded skill are
    auto-approved by the executor. v1 deliberately leaves the higher-
    level approval gate at the orchestrator's plan-approval step; we
    don't want a second confirmation per destructive sub-step inside
    one plan step."""


class RealExecutor(StepExecutor):
    """Bridges the agent's PlanStep onto a full SkillRunner invocation."""

    def __init__(self, config: RealExecutorConfig) -> None:
        self.config = config

    async def execute(
        self,
        step: PlanStep,
        skill: SkillFile,
        emit_progress: Callable[[str, dict[str, Any]], None],
    ) -> StepResult:
        skill_path = self._locate_skill_file(skill.id)
        if skill_path is None:
            return StepResult(
                succeeded=False,
                duration_ms=0,
                error_kind="missing_skill_file",
                error_message=f"no skill JSON for id {skill.id!r}",
            )

        try:
            v1_skill = self._load_v1_skill(skill_path)
        except Exception as e:  # noqa: BLE001
            return StepResult(
                succeeded=False,
                error_kind="skill_load_error",
                error_message=f"{type(e).__name__}: {e}",
            )

        emit_progress(
            "skill_invoke",
            {"test_id": skill.id, "screenshot_path": None},
        )

        # Run the synchronous SkillRunner in a worker thread.
        return await asyncio.to_thread(
            self._run_skill_sync, v1_skill, step.params
        )

    # ---- Helpers --------------------------------------------------------

    def _locate_skill_file(self, skill_id: str) -> Path | None:
        candidate = self.config.skills_dir / f"{skill_id}.json"
        if candidate.exists():
            return candidate
        # The v2 SkillFile may have synthesized id from `name`. Fall back
        # to a slugified name match.
        for p in self.config.skills_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if data.get("name", "").replace(" ", "_").lower() == skill_id:
                return p
        return None

    def _load_v1_skill(self, path: Path) -> Skill:
        return Skill.model_validate_json(path.read_text(encoding="utf-8"))

    def _run_skill_sync(
        self, skill: Skill, params: dict[str, Any]
    ) -> StepResult:
        start = time.time()
        session: BrowserSession | None = None
        try:
            session = connect_to_chrome(
                cdp_endpoint=self.config.cdp_endpoint,
                target_url_substring=self.config.target_url_substring,
            )
        except Exception as e:  # noqa: BLE001
            return StepResult(
                succeeded=False,
                duration_ms=int((time.time() - start) * 1000),
                error_kind="cdp_connect_failed",
                error_message=(
                    f"could not attach to Chrome at {self.config.cdp_endpoint}: "
                    f"{type(e).__name__}: {e}"
                ),
            )

        try:
            runner = SkillRunner(
                session=session,
                skill=skill,
                params=params,
                sessions_dir=self.config.sessions_dir,
                base_url=skill.base_url,
                approve_fn=(lambda _step: True)
                if self.config.auto_approve_gates
                else None,
                takeover_fn=lambda _step: False,  # never block in agent flow
            )
            results = runner.run()
        except Exception as e:  # noqa: BLE001
            return StepResult(
                succeeded=False,
                duration_ms=int((time.time() - start) * 1000),
                error_kind="skill_runner_crashed",
                error_message=f"{type(e).__name__}: {e}",
            )
        finally:
            session.close()

        # Aggregate. Any non-success step => failed plan step.
        last_shot = None
        last_error = None
        any_failure = False
        for _step, tool_result, _level in results:
            if tool_result.screenshot_path:
                last_shot = tool_result.screenshot_path
            if not tool_result.success:
                any_failure = True
                last_error = tool_result.error or tool_result.action_taken

        duration_ms = int((time.time() - start) * 1000)
        if any_failure:
            return StepResult(
                succeeded=False,
                duration_ms=duration_ms,
                error_kind="skill_step_failed",
                error_message=last_error or "one or more skill steps failed",
                screenshot_path=last_shot,
            )
        return StepResult(
            succeeded=True,
            duration_ms=duration_ms,
            screenshot_path=last_shot,
        )
