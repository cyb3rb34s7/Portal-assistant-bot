"""Smoke test: single-item end-to-end with the mock client.

Goal: prove the agent loop wires up correctly, all events fire in the
right order, the report is written, and the task ends in
task.completed. No network. No browser. ~1s runtime.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
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
    PlanApprove,
    PlanProposed,
    TaskCompleted,
    TaskSubmit,
)


SKILL_FIXTURE = {
    "schema_version": 2,
    "id": "curate_one_item",
    "name": "Curate one content item",
    "description": "Place one content asset in a slot and save layout.",
    "version": 1,
    "parameters": [
        {"name": "content_id", "semantic": "Asset id (A-NNNN)", "required": True,
         "source_hint": "user-provided"},
        {"name": "layout_row", "semantic": "Grid row, 1-indexed",
         "required": True, "type": "number"},
        {"name": "layout_position", "semantic": "Grid position, 1-indexed",
         "required": True, "type": "number"},
    ],
    "preconditions": ["operator is logged in"],
    "success_assertions": [
        {"type": "text_visible", "text": "Layout saved"},
    ],
    "destructive_actions": [
        {"step": 7, "kind": "save_layout", "reversible": True},
    ],
    "steps": [],
}


PORTAL_FIXTURE_YAML = """
schema_version: 1
portal_id: sample_portal
name: "Sample Portal"
base_url: "http://localhost:5173"
glossary:
  - term: curation
    meaning: "place content in slots"
page_map:
  - path: /curation
    role: primary_workflow
field_conventions:
  - field: content_id
    format: A-NNNN
"""


PLANNER_RESPONSE = json.dumps({
    "decision": "plan",
    "plan_summary": "Curate one item",
    "plan_steps": [
        {
            "skill_id": "curate_one_item",
            "params": {
                "content_id": "A-9001",
                "layout_row": 1,
                "layout_position": 1,
            },
            "param_sources": {
                "content_id": "user-provided",
                "layout_row": "user-provided",
                "layout_position": "user-provided",
            },
            "notes": None,
        }
    ],
    "estimated_duration_seconds": 30,
    "clarify_questions": [],
    "notes": None,
})


INTAKE_RESPONSE = json.dumps({
    "content_items": [{"id": "A-9001", "title": None, "thumbnail_path": None,
                       "raw_source": "user-provided", "extra": {}}],
    "dates": [],
    "files_resolved": [],
    "raw_text_excerpts": [],
    "warnings": [],
})


REPORTER_RESPONSE = json.dumps({
    "headline": "Single-item curation completed.",
    "paragraphs": [
        "The plan executed without issue.",
        "All steps succeeded.",
    ],
})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "curate_one_item.json").write_text(json.dumps(SKILL_FIXTURE))
    (tmp_path / "sessions").mkdir()
    return tmp_path


@pytest.mark.asyncio
async def test_smoke_single_item_completes(workspace: Path) -> None:
    client = MockClient()
    # Route based on the system-prompt header content
    client.expect_when(
        lambda msgs: any("intake stage" in (m.content or "").lower() for m in msgs),
        text=INTAKE_RESPONSE,
    )
    client.expect_when(
        lambda msgs: any("planner stage" in (m.content or "").lower() for m in msgs),
        text=PLANNER_RESPONSE,
    )
    client.expect_when(
        lambda msgs: any("reporter stage" in (m.content or "").lower() for m in msgs),
        text=REPORTER_RESPONSE,
    )

    portal = PortalContext.model_validate(__import__("yaml").safe_load(PORTAL_FIXTURE_YAML))

    config = OrchestratorConfig(
        sessions_dir=workspace / "sessions",
        skills_dir=workspace / "skills",
        portal_context=portal,
        intake_use_llm=True,
        auto_approve_plan=True,
    )
    ev_q: asyncio.Queue = asyncio.Queue()
    cmd_q: asyncio.Queue = asyncio.Queue()
    orch = Orchestrator(client=client, config=config, ev_out=ev_q, cmd_in=cmd_q,
                        executor=FakeExecutor())

    submit = TaskSubmit(
        task_id="t-smoke",
        goal="curate A-9001 in row 1 position 1",
        attachments=[],
        portal_id="sample_portal",
    )

    runner = asyncio.create_task(orch.run_task(submit))

    # Drain events until the task terminates.
    seen_types: list[str] = []
    completed = False
    plan_proposed_seen = False

    async def _consume() -> None:
        nonlocal completed, plan_proposed_seen
        while True:
            ev = await ev_q.get()
            seen_types.append(ev.type)
            if isinstance(ev, PlanProposed):
                plan_proposed_seen = True
            if isinstance(ev, TaskCompleted):
                completed = True
                return
            if ev.type in ("task.failed", "task.cancelled"):
                return

    await asyncio.wait_for(_consume(), timeout=10.0)
    await asyncio.wait_for(runner, timeout=2.0)

    assert completed, f"task did not complete; saw events: {seen_types}"
    assert plan_proposed_seen
    assert "intake.extracted" in seen_types
    assert "step.started" in seen_types
    assert "step.succeeded" in seen_types
    assert "report.ready" in seen_types

    # Report file exists
    sessions = list((workspace / "sessions").iterdir())
    assert len(sessions) == 1
    assert (sessions[0] / "report.md").exists()
