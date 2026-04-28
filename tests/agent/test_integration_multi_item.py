"""Multi-item integration test: builds a CSV of 3 items, drives the
agent through intake + planner + (auto-approved) plan + 3 fake step
executions + report. Exercises:

  - CSV pre-pass extraction
  - planner emitting one plan_step per item
  - destructive_actions surfaced from skill metadata
  - mid-loop pause+retry on a configured-to-fail step

Still uses the MockClient — no network required. The Groq-backed
integration is in test_groq_live.py and only runs when GROQ_API_KEY
is set.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pilot.agent.ai_client.adapters.mock import MockClient
from pilot.agent.orchestrator import (
    FakeExecutor,
    Orchestrator,
    OrchestratorConfig,
)
from pilot.agent.schemas.portal_context import PortalContext
from pilot.agent.schemas.protocol import (
    Attachment,
    Paused,
    PauseResolve,
    StepFailedEvent,
    TaskCompleted,
    TaskFailed,
    TaskSubmit,
)


SKILL_FIXTURE = {
    "schema_version": 2,
    "id": "curate_one_item",
    "name": "Curate one content item",
    "description": "Place content in a slot.",
    "version": 1,
    "parameters": [
        {"name": "content_id", "required": True, "source_hint": "csv col 'id'"},
        {"name": "layout_row", "required": True, "type": "number"},
        {"name": "layout_position", "required": True, "type": "number"},
    ],
    "destructive_actions": [
        {"step": 1, "kind": "save_layout", "reversible": True},
    ],
}

PORTAL = PortalContext(
    portal_id="sample_portal",
    name="Sample",
    base_url="http://localhost:5173",
)

INTAKE_REPLY = json.dumps({
    "content_items": [
        {"id": "A-1001", "raw_source": "csv row 1"},
        {"id": "A-1002", "raw_source": "csv row 2"},
        {"id": "A-1003", "raw_source": "csv row 3"},
    ],
    "dates": [],
    "files_resolved": [],
    "raw_text_excerpts": ["CSV headers: id, row, position"],
    "warnings": [],
})

PLANNER_REPLY = json.dumps({
    "decision": "plan",
    "plan_summary": "Curate three items",
    "plan_steps": [
        {"skill_id": "curate_one_item",
         "params": {"content_id": "A-1001", "layout_row": 1, "layout_position": 1},
         "param_sources": {"content_id": "csv col id", "layout_row": "csv col row",
                          "layout_position": "csv col position"}},
        {"skill_id": "curate_one_item",
         "params": {"content_id": "A-1002", "layout_row": 1, "layout_position": 2},
         "param_sources": {"content_id": "csv col id"}},
        {"skill_id": "curate_one_item",
         "params": {"content_id": "A-1003", "layout_row": 1, "layout_position": 3},
         "param_sources": {"content_id": "csv col id"}},
    ],
    "estimated_duration_seconds": 90,
    "clarify_questions": [],
})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "curate_one_item.json").write_text(json.dumps(SKILL_FIXTURE))
    csv_path = tmp_path / "items.csv"
    csv_path.write_text("id,row,position\nA-1001,1,1\nA-1002,1,2\nA-1003,1,3\n")
    (tmp_path / "sessions").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_multi_item_with_failure_and_retry(workspace: Path) -> None:
    client = MockClient()
    client.expect_when(
        lambda msgs: any("intake stage" in (m.content or "").lower() for m in msgs),
        text=INTAKE_REPLY,
    )
    client.expect_when(
        lambda msgs: any("planner stage" in (m.content or "").lower() for m in msgs),
        text=PLANNER_REPLY,
    )
    client.expect_text("(no reporter)")

    # Configure step 2 to fail once, then succeed on retry.
    class OneTimeFailExecutor(FakeExecutor):
        def __init__(self) -> None:
            super().__init__()
            self._failed_once = False

        async def execute(self, step, skill, emit_progress):  # type: ignore[override]
            if step.idx == 2 and not self._failed_once:
                self._failed_once = True
                from pilot.agent.orchestrator import StepResult

                return StepResult(
                    succeeded=False,
                    error_kind="locator_exhausted",
                    error_message="(test) configured to fail once",
                )
            return await super().execute(step, skill, emit_progress)

    config = OrchestratorConfig(
        sessions_dir=workspace / "sessions",
        skills_dir=workspace / "skills",
        portal_context=PORTAL,
        intake_use_llm=True,
        auto_approve_plan=True,
    )
    ev_q: asyncio.Queue = asyncio.Queue()
    cmd_q: asyncio.Queue = asyncio.Queue()
    orch = Orchestrator(
        client=client, config=config, ev_out=ev_q, cmd_in=cmd_q,
        executor=OneTimeFailExecutor(),
    )
    submit = TaskSubmit(
        task_id="t-multi",
        goal="curate three items from the csv",
        attachments=[Attachment(path=str(workspace / "items.csv"), kind="csv")],
        portal_id="sample_portal",
    )
    runner = asyncio.create_task(orch.run_task(submit))

    seen: list[str] = []
    succeeded_idxs: list[int] = []
    failed_idxs: list[int] = []
    completed = False

    async def _consume() -> None:
        nonlocal completed
        while True:
            ev = await ev_q.get()
            seen.append(ev.type)
            if isinstance(ev, Paused):
                # Resolve the pause with a retry.
                await cmd_q.put(
                    PauseResolve(
                        task_id="t-multi", pause_id=ev.pause_id, action="retry"
                    )
                )
            elif ev.type == "step.succeeded":
                succeeded_idxs.append(ev.idx)  # type: ignore[attr-defined]
            elif isinstance(ev, StepFailedEvent):
                failed_idxs.append(ev.idx)
            elif isinstance(ev, TaskCompleted):
                completed = True
                return
            elif isinstance(ev, TaskFailed):
                return

    await asyncio.wait_for(_consume(), timeout=15.0)
    await asyncio.wait_for(runner, timeout=2.0)

    assert completed, f"task did not complete; events: {seen}"
    # Step 2 failed once then succeeded on retry; expect 1 fail event,
    # and steps 1, 2, 3 all eventually succeeded.
    assert sorted(set(succeeded_idxs)) == [1, 2, 3]
    assert 2 in failed_idxs
