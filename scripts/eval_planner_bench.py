"""Phase 4 / 5 planner evaluation bench.

Runs a fixed list of goals against the live planner and scores each:
- did it produce a Plan (or did it correctly clarify)?
- did the plan use the expected skill?
- did the plan extract the expected param values from goal + CSV?

Usage:
    .venv/bin/python scripts/eval_planner_bench.py --client groq
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pilot.agent.ai_client import get_client
from pilot.agent.intake import run_intake
from pilot.agent.planner import load_skill_library, run_planner
from pilot.agent.schemas.portal_context import PortalContext
from pilot.agent.schemas.protocol import Attachment


REPO = Path(__file__).resolve().parent.parent


@dataclass
class Case:
    name: str
    goal: str
    attachments: list[str] = field(default_factory=list)
    # If `expect_plan` is True, the planner must produce a plan; if
    # False, it must emit at least one clarify question.
    expect_plan: bool = True
    expect_skill_id: str | None = None
    expect_params_subset: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


CASES: list[Case] = [
    Case(
        name="P1-explicit-featured-row",
        goal=(
            "Curate a featured-row layout from this CSV. Comment: "
            "'Spring drop hero row'."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expect_plan=True,
        expect_skill_id="curate_featured_row",
        expect_params_subset={"slot_1_content_id": "A-9001"},
    ),
    Case(
        name="P2-grid-2x2-with-comment",
        goal=(
            "Build a grid-2x2 layout from the attached CSV with comment "
            "'New collection grid'."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expect_plan=True,
        expect_skill_id="curate_grid_2x2",
    ),
    Case(
        name="P3-carousel-five-slots",
        goal=(
            "Use the carousel layout (5 slots) and curate all contents "
            "from the CSV. Comment: 'May carousel'."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expect_plan=True,
        expect_skill_id="curate_carousel",
    ),
    Case(
        name="C1-no-layout-specified",
        goal="Curate the contents from this CSV. Comment: 'auto pick'.",
        attachments=["tests/fixtures/batch.csv"],
        expect_plan=False,
        notes="layout is ambiguous; should clarify which of the three.",
    ),
    Case(
        name="C2-no-csv-attached",
        goal="Curate a featured-row layout. Comment: 'Spring drop'.",
        attachments=[],
        expect_plan=False,
        notes="no CSV; should clarify what content goes in slots.",
    ),
]


async def run_one(client, skills, portal, case: Case) -> dict[str, Any]:
    attachments = [
        Attachment(path=str(REPO / a), kind="csv" if a.endswith(".csv") else None)
        for a in case.attachments
    ]
    entities = await run_intake(
        client=client,
        goal=case.goal,
        attachments=attachments,
        use_llm=False,
    )
    out = await run_planner(
        client=client,
        goal=case.goal,
        entities=entities,
        skills=skills,
        portal=portal,
    )

    got_plan = out.plan is not None
    score = {
        "case": case.name,
        "expected": "plan" if case.expect_plan else "clarify",
        "got": "plan" if got_plan else "clarify",
        "decision_correct": got_plan == case.expect_plan,
        "skill_correct": None,
        "params_correct": None,
        "clarify_questions": [q.question for q in out.clarify_questions],
        "first_step_params": (
            out.plan.steps[0].params if got_plan and out.plan.steps else None
        ),
        "notes": out.notes or case.notes or "",
    }
    if got_plan and case.expect_plan:
        first = out.plan.steps[0] if out.plan.steps else None
        score["skill_correct"] = (
            first is not None and first.skill_id == case.expect_skill_id
        )
        if case.expect_params_subset:
            score["params_correct"] = all(
                str(first.params.get(k)) == str(v)
                for k, v in case.expect_params_subset.items()
            )
    return score


async def amain(args: argparse.Namespace) -> int:
    skills = load_skill_library(REPO / "skills")
    portal_path = REPO / "portals" / "sample_portal" / "context.yaml"
    portal = None
    if portal_path.exists():
        import yaml

        portal = PortalContext.model_validate(yaml.safe_load(portal_path.read_text()))

    client = get_client(args.client)
    results = []
    for case in CASES:
        try:
            r = await run_one(client, skills, portal, case)
        except Exception as e:  # noqa: BLE001
            r = {"case": case.name, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(json.dumps(r, indent=2))
    await client.close()

    # Summary
    passed = sum(
        1 for r in results
        if r.get("decision_correct")
        and (r.get("skill_correct") in (True, None))
        and (r.get("params_correct") in (True, None))
    )
    print(f"\n=== {passed}/{len(results)} cases passed ===")
    return 0 if passed == len(results) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--client", default="groq")
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
