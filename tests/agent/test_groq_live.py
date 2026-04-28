"""Live integration test against Groq.

Skipped automatically unless ``GROQ_API_KEY`` is set in the environment.

Verifies that the AIClient + structured-output helper produces a valid
plan for a tiny goal end-to-end. Single LLM call, mindful of free-tier
rate limits.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from pilot.agent.ai_client import get_client
from pilot.agent.intake import run_intake
from pilot.agent.planner import load_skill_library, run_planner
from pilot.agent.schemas.portal_context import PortalContext


SKILL_FIXTURE = {
    "schema_version": 2,
    "id": "curate_one_item",
    "name": "Curate one content item",
    "description": "Place an asset in a layout slot.",
    "version": 1,
    "parameters": [
        {"name": "content_id", "semantic": "Asset id", "required": True},
        {"name": "layout_row", "semantic": "Grid row (1-indexed)",
         "required": True, "type": "number"},
        {"name": "layout_position", "semantic": "Grid position",
         "required": True, "type": "number"},
    ],
}

PORTAL = PortalContext(
    portal_id="sample_portal",
    name="Sample",
    base_url="http://localhost:5173",
)


pytestmark = pytest.mark.skipif(
    "GROQ_API_KEY" not in os.environ,
    reason="GROQ_API_KEY not set; skipping live integration test",
)


@pytest.mark.asyncio
async def test_groq_planner_produces_plan(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "curate_one_item.json").write_text(json.dumps(SKILL_FIXTURE))
    skills = load_skill_library(skills_dir)

    client = get_client("groq")

    entities = await run_intake(
        client=client,
        goal="Curate asset A-9001 in row 1 position 2",
        attachments=[],
        use_llm=False,  # deterministic only - test the planner
    )
    out = await run_planner(
        client=client,
        goal="Curate asset A-9001 in row 1 position 2",
        entities=entities,
        skills=skills,
        portal=PORTAL,
    )
    await client.close()

    assert out.plan is not None, (
        f"expected a plan, got clarify questions: {out.clarify_questions}"
    )
    assert len(out.plan.steps) == 1
    assert out.plan.steps[0].skill_id == "curate_one_item"
    params = out.plan.steps[0].params
    # The planner must have parsed A-9001 into content_id and a positive
    # row/position into layout_row / layout_position.
    assert params.get("content_id") == "A-9001"
    assert int(params.get("layout_row")) == 1
    assert int(params.get("layout_position")) == 2
