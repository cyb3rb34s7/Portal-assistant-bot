"""Real `StepExecutor` that drives the deterministic `pilot.skill_runner`.

One orchestrator-level step in our agent corresponds to **one full skill
invocation** (planner says: "use skill `curate_layout` with these
params"). We load the v1 Skill JSON from disk, connect to Chrome over
CDP, and let `SkillRunner` replay the recorded trace using the
4-level locator fallback.

Process model
-------------

The orchestrator is async; `SkillRunner` is sync (built on
`sync_playwright`). We bridge by running the runner on a **dedicated
single-thread executor** so:

  - successive plan steps run on the **same** thread, which means we
    can **reuse the BrowserSession** across steps (a 12-step plan now
    attaches to CDP once instead of 12 times);
  - sync_playwright's per-thread invariants are respected (its driver
    state is bound to the thread that started it).

The orchestrator calls ``RealExecutor.close()`` in its task ``finally``
block; that closes the session and shuts down the worker thread.

If a single skill run crashes, we drop the session before returning so
the next step starts cleanly. The next ``execute()`` will re-attach.

The first ``execute()`` chooses the CDP target tab using, in order:
  1. ``RealExecutorConfig.target_url_substring`` (operator-supplied),
  2. the recorded skill's ``base_url`` (so multi-tab Chrome attaches to
     the actual portal tab, not whichever one was opened first).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
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


def _alts_equivalent(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two fingerprints are 'the same alternate' if they pin the same
    element by any stable identity. Used to de-dupe alternates so the
    skill JSON doesn't grow unboundedly across reruns."""
    if a.get("test_id") and a.get("test_id") == b.get("test_id"):
        return True
    if a.get("element_id") and a.get("element_id") == b.get("element_id"):
        return True
    if (
        a.get("role")
        and a.get("accessible_name")
        and a.get("role") == b.get("role")
        and a.get("accessible_name") == b.get("accessible_name")
    ):
        return True
    if a.get("xpath") and a.get("xpath") == b.get("xpath"):
        return True
    return False


@dataclass
class RealExecutorConfig:
    skills_dir: Path
    sessions_dir: Path
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT
    target_url_substring: str | None = None
    """Optional operator-supplied substring used to pick the right tab
    when multiple are open. If unset, the executor falls back to the
    skill's ``base_url`` on first attach."""

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
        # Single-thread pool so all skill invocations share one OS thread
        # and one sync_playwright driver.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="real-executor"
        )
        self._session: BrowserSession | None = None

    # ---- Public API ----------------------------------------------------

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

        # Translate semantic param names (planner-emitted) back to v1
        # binding names (recorded trace). The alias map lives on the
        # SkillFile, populated from the .v2.json sidecar by
        # load_skill_library — no module-level state.
        runtime_params = self._translate_params(skill, step.params)

        # Pre-flight check: any file_path parameter must point at a
        # file that actually exists. Without this the failure surfaces
        # late as "could not click apply" (Phase 6 limit 6.4).
        missing = self._check_file_paths(skill, runtime_params)
        if missing:
            return StepResult(
                succeeded=False,
                duration_ms=0,
                error_kind="missing_file",
                error_message=(
                    "file_path param(s) not found on disk: "
                    + ", ".join(f"{k}={v!r}" for k, v in missing)
                ),
            )

        # Run on the dedicated worker thread so we can reuse the
        # BrowserSession across steps. ``skill_path`` is resolved
        # *here* (where we have the v2 SkillFile with its ``id`` field)
        # and threaded through, because the v1 Skill loaded from disk
        # does not carry the id and the worker can't re-resolve it.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool,
            self._run_skill_sync,
            v1_skill,
            runtime_params,
            skill.base_url,
            skill_path,
        )

    def close(self) -> None:
        """Close the long-lived BrowserSession (if any) and shut down
        the worker pool. Called by the orchestrator when a task ends.
        """
        # Closing the session must run on the worker thread that owns
        # the sync_playwright driver, otherwise we hit thread-affinity
        # errors deep inside Playwright.
        try:
            fut = self._pool.submit(self._close_session_in_worker)
            fut.result(timeout=10)
        except Exception:
            pass
        self._pool.shutdown(wait=False)

    # ---- Param translation + file-path checks --------------------------

    def _translate_params(
        self, skill: SkillFile, params: dict[str, Any]
    ) -> dict[str, Any]:
        alias = skill.param_alias_map
        if not alias:
            return dict(params)
        out: dict[str, Any] = {}
        for k, v in params.items():
            out[alias.get(k, k)] = v
        return out

    def _check_file_paths(
        self, skill: SkillFile, runtime_params: dict[str, Any]
    ) -> list[tuple[str, Any]]:
        """Return list of (param_name, value) pairs whose param is
        declared file_path but does not exist on disk. Resolved against
        the SkillFile's parameters list. The runtime_params dict is
        keyed by v1 binding names (post-alias-translation), so we map
        through the alias to find the corresponding v2 type."""
        if not skill.parameters:
            return []
        # Build inverse: v1 binding -> declared v2 param
        v1_to_v2: dict[str, Any] = {}
        for p in skill.parameters:
            v1_name = skill.param_alias_map.get(p.name, p.name)
            v1_to_v2[v1_name] = p

        missing: list[tuple[str, Any]] = []
        for k, v in runtime_params.items():
            param_def = v1_to_v2.get(k)
            if param_def is None or param_def.type != "file_path":
                continue
            if v in (None, ""):
                continue
            if not Path(str(v)).exists():
                missing.append((k, v))
        return missing

    # ---- Skill loading -------------------------------------------------

    def _locate_skill_file(self, skill_id: str) -> Path | None:
        candidate = self.config.skills_dir / f"{skill_id}.json"
        if candidate.exists():
            return candidate
        # The v2 SkillFile may have synthesized id from `name`. Fall back
        # to a slugified name match.
        for p in self.config.skills_dir.glob("*.json"):
            if p.name.endswith(".v2.json"):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if data.get("name", "").replace(" ", "_").lower() == skill_id:
                return p
        return None

    def _load_v1_skill(self, path: Path) -> Skill:
        return Skill.model_validate_json(path.read_text(encoding="utf-8"))

    # ---- Worker-thread methods (own the BrowserSession) ----------------

    def _ensure_session(self, default_base_url: str | None) -> BrowserSession:
        """Create-or-reuse the BrowserSession. Runs on the worker thread."""
        if self._session is not None:
            return self._session
        target = self.config.target_url_substring or default_base_url
        self._session = connect_to_chrome(
            cdp_endpoint=self.config.cdp_endpoint,
            target_url_substring=target,
        )
        return self._session

    def _close_session_in_worker(self) -> None:
        """Run on the worker thread; closes the session it owns."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def _run_skill_sync(
        self,
        skill: Skill,
        params: dict[str, Any],
        default_base_url: str | None,
        skill_path: Path | None = None,
    ) -> StepResult:
        start = time.time()
        try:
            session = self._ensure_session(default_base_url or skill.base_url)
        except Exception as e:  # noqa: BLE001
            self._session = None
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
            # Drop the session on a crash; the next step will re-attach.
            self._close_session_in_worker()
            return StepResult(
                succeeded=False,
                duration_ms=int((time.time() - start) * 1000),
                error_kind="skill_runner_crashed",
                error_message=f"{type(e).__name__}: {e}",
            )
        # Aggregate. Any non-success step => failed plan step.
        last_shot = None
        last_error = None
        any_failure = False
        heals: list[dict[str, Any]] = []
        # Persist successful, high-confidence heals back into the skill
        # JSON: each healed sub-step gets its new fingerprint appended
        # to the original step's `alternates` list. Next replay hits L1.
        persist_targets: list[tuple[int, dict[str, Any]]] = []  # (step.index, healed_dict)
        for sub_step, tool_result, _level in results:
            if tool_result.screenshot_path:
                last_shot = tool_result.screenshot_path
            if not tool_result.success:
                any_failure = True
                last_error = tool_result.error or tool_result.action_taken
            if tool_result.healed:
                healed = dict(tool_result.healed)  # copy so we can mutate
                healed.setdefault("step_index", sub_step.index)
                heals.append(healed)
                if (
                    tool_result.success
                    and healed.get("confidence") == "high"
                    and healed.get("post_condition_passed")
                    and healed.get("new_fingerprint")
                ):
                    persist_targets.append((sub_step.index, healed))

        # Write persistence targets back to the skill JSON file. Done
        # only after all sub-steps complete so a single mid-skill crash
        # doesn't leave the JSON half-written. ``skill_path`` was
        # resolved by the caller (execute()) where the v2 SkillFile.id
        # is available; we accept it as a parameter so this worker
        # method doesn't need to know about v2 vs v1 schema.
        if persist_targets and skill_path is not None:
            persisted_count = self._persist_alternates_to_skill(
                skill_path, persist_targets
            )
            if persisted_count:
                # Mark each persisted heal so the orchestrator's
                # StepHealedEvent reflects the correct flag.
                for _, healed in persist_targets[:persisted_count]:
                    healed["persisted_to_skill"] = True

        duration_ms = int((time.time() - start) * 1000)
        if any_failure:
            return StepResult(
                succeeded=False,
                duration_ms=duration_ms,
                error_kind="skill_step_failed",
                error_message=last_error or "one or more skill steps failed",
                screenshot_path=last_shot,
                heals=heals,
            )
        return StepResult(
            succeeded=True,
            duration_ms=duration_ms,
            screenshot_path=last_shot,
            heals=heals,
        )

    # ---- Skill JSON write-back -----------------------------------------

    def _persist_alternates_to_skill(
        self,
        skill_path: Path,
        persist_targets: list[tuple[int, dict[str, Any]]],
    ) -> int:
        """Append healed alternate fingerprints to the matching steps in
        the skill JSON on disk. Returns the count of successfully
        appended alternates. Failures are tolerated — we don't want a
        write-back error to fail an otherwise successful replay.
        """
        try:
            data = json.loads(skill_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        steps = data.get("steps")
        if not isinstance(steps, list):
            return 0

        appended = 0
        for step_idx, healed in persist_targets:
            new_fp = healed.get("new_fingerprint")
            if not new_fp:
                continue
            try:
                target_step = steps[step_idx]
            except (IndexError, TypeError):
                continue
            if not isinstance(target_step, dict):
                continue
            fp = target_step.get("fingerprint")
            if not isinstance(fp, dict):
                continue
            alts = fp.setdefault("alternates", [])
            if not isinstance(alts, list):
                continue
            # Dedupe: skip if an alternate with the same testid /
            # accessible_name+role already exists, so repeated runs
            # don't bloat the file.
            if any(
                _alts_equivalent(a, new_fp) for a in alts if isinstance(a, dict)
            ):
                continue
            alts.append(new_fp)
            appended += 1

        if appended == 0:
            return 0
        try:
            skill_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            return 0
        return appended
