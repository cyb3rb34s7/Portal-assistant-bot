"""Phase 6.9 - clarification quality bench.

Distinguishes between:
  - SHOULD-PLAN goals where the planner mistakenly clarifies (false-clarify);
  - SHOULD-CLARIFY goals where the planner correctly clarifies AND
    the question is high-quality (specific, addresses the actual ambiguity);
  - SHOULD-CLARIFY where the planner clarifies but the question is filler
    (asks about already-specified info).

Each case has a targeted assertion list.

Usage:
    .venv/bin/python scripts/eval_phase6_clarify_quality.py --client groq
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
class ClarifyCase:
    name: str
    goal: str
    attachments: list[str] = field(default_factory=list)
    expect: str = "plan"  # "plan" or "clarify"
    must_mention: list[str] = field(default_factory=list)
    must_not_mention: list[str] = field(default_factory=list)
    notes: str = ""


CASES: list[ClarifyCase] = [
    # -------- Should-plan: false-clarify is a fail. --------
    ClarifyCase(
        name="Q1-fully-specified-featured",
        goal=(
            "Curate a featured-row layout from this CSV. "
            "Comment: 'Spring drop'."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expect="plan",
        must_not_mention=["comment", "layout"],
        notes="goal quotes comment AND names layout; planner must not re-ask either.",
    ),
    ClarifyCase(
        name="Q2-fully-specified-grid",
        goal=(
            "Build a grid-2x2 layout using the attached CSV. "
            "Comment: 'q2 test'."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expect="plan",
        must_not_mention=["comment", "layout"],
    ),

    # -------- Should-clarify: question must address the actual gap. --------
    ClarifyCase(
        name="Q3-no-layout-must-ask-layout",
        goal="Curate the contents from this CSV. Comment: 'auto pick'.",
        attachments=["tests/fixtures/batch.csv"],
        expect="clarify",
        must_mention=["layout"],
        must_not_mention=["csv", "comment"],
        notes="layout is the missing piece; comment is supplied so don't ask",
    ),
    ClarifyCase(
        name="Q4-no-csv-must-ask-csv-or-content",
        goal="Curate a featured-row layout. Comment: 'Spring drop'.",
        attachments=[],
        expect="clarify",
        must_mention=["csv", "content"],
        notes="should ask for content source; layout + comment supplied",
    ),
    ClarifyCase(
        name="Q5-vague-goal-must-ask-multiple",
        goal="Help me set up the portal.",
        attachments=[],
        expect="clarify",
        must_mention=["layout", "content"],
        notes="extremely vague; multiple gaps need clarification",
    ),

    # -------- Edge: should-plan with extra context that could trigger over-clarify. --------
    ClarifyCase(
        name="Q6-comment-with-special-chars",
        goal=(
            "Featured-row layout from this CSV. "
            'Comment: "Q4\'25 plan -- \'limited\' edition!"'
        ),
        attachments=["tests/fixtures/batch.csv"],
        expect="plan",
        must_not_mention=["comment"],
        notes="quoted comment with apostrophes/punctuation -- must not re-ask",
    ),
    ClarifyCase(
        name="Q7-redundant-info-ok",
        goal=(
            "Curate a featured-row layout. The comment should be 'Spring'. "
            "Use the attached CSV."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expect="plan",
        must_not_mention=["comment", "layout"],
        notes="info is split across sentences -- planner must still resolve.",
    ),
    ClarifyCase(
        name="Q8-different-layout-keyword",
        goal=(
            "Use the carousel for this CSV. Comment: 'May test'."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expect="plan",
        must_not_mention=["comment", "layout"],
        notes="just 'carousel', not 'carousel layout' -- still unambiguous",
    ),
]


def _score(out, case: ClarifyCase) -> tuple[bool, str]:
    got_plan = out.plan is not None
    if case.expect == "plan":
        if not got_plan:
            return False, (
                f"FALSE-CLARIFY: planner asked instead of planning. "
                f"Questions: {[q.question for q in out.clarify_questions]}"
            )
        return True, "plan emitted as expected"

    # case.expect == "clarify"
    if got_plan:
        return False, "expected clarify, got plan"
    questions = [q.question.lower() for q in out.clarify_questions]
    joined = " | ".join(questions)
    for kw in case.must_mention:
        if kw.lower() not in joined:
            return False, (
                f"clarify questions don't mention required keyword "
                f"{kw!r}: {joined}"
            )
    for kw in case.must_not_mention:
        if kw.lower() in joined:
            return False, (
                f"clarify questions ASK about already-specified {kw!r} "
                f"(filler question): {joined}"
            )
    return True, f"clarify well-targeted: {joined}"


async def run_one(client, skills, portal, case: ClarifyCase) -> dict[str, Any]:
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
    passed, reason = _score(out, case)
    return {
        "case": case.name,
        "expected": case.expect,
        "got": "plan" if out.plan else "clarify",
        "passed": passed,
        "reason": reason,
        "questions": [q.question for q in out.clarify_questions],
    }


async def amain(args: argparse.Namespace) -> int:
    skills = load_skill_library(REPO / "skills")
    portal_path = REPO / "portals" / "sample_portal" / "context.yaml"
    portal = None
    if portal_path.exists():
        import yaml

        portal = PortalContext.model_validate(yaml.safe_load(portal_path.read_text()))

    client = get_client(args.client)
    results = []
    cases = CASES if not args.only else [c for c in CASES if c.name == args.only]
    for case in cases:
        try:
            r = await run_one(client, skills, portal, case)
        except Exception as e:  # noqa: BLE001
            r = {"case": case.name, "passed": False, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(json.dumps(r, indent=2))
    await client.close()

    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    print(f"\n=== Phase 6 clarify quality: {passed}/{total} cases passed ===")
    for r in results:
        mark = "PASS" if r.get("passed") else "FAIL"
        print(f"  [{mark}] {r['case']}: {r.get('reason', r.get('error', ''))[:200]}")
    return 0 if passed == total else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--client", default="groq")
    p.add_argument("--only", default=None)
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
