"""Per-stage eval harness.

Runs the planner stage with a fixed goal + skill library against a
configurable list of (client, model) pairs and prints results side by
side.

Usage:
    python scripts/eval_planner.py --client groq \
        --model llama-3.3-70b-versatile \
        --goal "Curate A-9001 into row 1 position 2"

Add multiple (--client, --model) pairs to compare.

Costs and rate limits: each call is one LLM round-trip. Default goal
fits in ~1k input tokens; budget accordingly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from pilot.agent.ai_client import get_client
from pilot.agent.intake import run_intake
from pilot.agent.planner import load_skill_library, run_planner
from pilot.agent.schemas.portal_context import PortalContext


DEFAULT_SKILLS = [
    {
        "schema_version": 2,
        "id": "curate_one_item",
        "name": "Curate one content item",
        "description": "Place an asset in a slot and save layout.",
        "version": 1,
        "parameters": [
            {"name": "content_id", "semantic": "Asset id", "required": True},
            {"name": "layout_row", "semantic": "Grid row 1-indexed",
             "required": True, "type": "number"},
            {"name": "layout_position", "semantic": "Grid position 1-indexed",
             "required": True, "type": "number"},
        ],
    },
]


async def run_one(*, client_name: str, model: str | None, goal: str,
                  skills_dir: Path, portal: PortalContext | None) -> dict:
    skills = load_skill_library(skills_dir)
    client = get_client(client_name)
    start = time.time()
    entities = await run_intake(
        client=client, goal=goal, attachments=[], use_llm=False,
    )
    out = await run_planner(
        client=client, goal=goal, entities=entities, skills=skills,
        portal=portal, model=model,
    )
    elapsed_ms = int((time.time() - start) * 1000)
    await client.close()
    return {
        "client": client_name,
        "model": model or "(default)",
        "elapsed_ms": elapsed_ms,
        "plan_steps": len(out.plan.steps) if out.plan else 0,
        "clarify_count": len(out.clarify_questions),
        "first_step_params": (
            out.plan.steps[0].params if out.plan and out.plan.steps else None
        ),
        "notes": out.notes,
    }


async def main_async(args: argparse.Namespace) -> int:
    skills_dir = Path(args.skills_dir)
    if not skills_dir.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "curate_one_item.json").write_text(json.dumps(DEFAULT_SKILLS[0]))

    portal = None
    if args.portal:
        import yaml
        p = Path("portals") / args.portal / "context.yaml"
        if p.exists():
            portal = PortalContext.model_validate(yaml.safe_load(p.read_text()))

    pairs = list(zip(args.client, args.model))
    if not pairs:
        pairs = [("mock", None)]

    results = []
    for client_name, model in pairs:
        try:
            r = await run_one(
                client_name=client_name, model=model, goal=args.goal,
                skills_dir=skills_dir, portal=portal,
            )
        except Exception as e:  # noqa: BLE001
            r = {"client": client_name, "model": model, "error": f"{type(e).__name__}: {e}"}
        results.append(r)

    print(json.dumps(results, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--client", action="append", default=[])
    p.add_argument("--model", action="append", default=[])
    p.add_argument("--goal", default="Curate A-9001 in row 1 position 2")
    p.add_argument("--skills-dir", default="tmp_skills")
    p.add_argument("--portal", default=None)
    args = p.parse_args()
    if len(args.client) != len(args.model):
        # pad with None so client without --model uses default
        while len(args.model) < len(args.client):
            args.model.append(None)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
