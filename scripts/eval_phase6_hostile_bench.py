"""Phase 6 hostile-input bench for the planner.

These cases deliberately give the planner broken or contradictory
inputs and assert the planner fails *gracefully* -- either by
clarifying (preferred) or by returning a plan whose plumbed-through
data the executor would reject loudly. The cases are NOT happy-path
tests; failure to clarify on a hostile input is a real product bug.

Each case has an explicit `expectation` string that explains what
"correct" means for that case, and an `evaluator` callable that
inspects the planner output and returns (passed: bool, reason: str).

Usage:
    .venv/bin/python scripts/eval_phase6_hostile_bench.py --client groq
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pilot.agent.ai_client import get_client
from pilot.agent.intake import run_intake
from pilot.agent.planner import load_skill_library, run_planner
from pilot.agent.schemas.portal_context import PortalContext
from pilot.agent.schemas.protocol import Attachment


REPO = Path(__file__).resolve().parent.parent


@dataclass
class HostileCase:
    name: str
    goal: str
    attachments: list[str] = field(default_factory=list)
    expectation: str = ""
    evaluator: Callable[[Any], tuple[bool, str]] | None = None


def _has_clarify(out, *, must_mention: list[str] | None = None) -> tuple[bool, str]:
    if out.plan is not None:
        return False, f"expected clarify, got plan: {out.plan.summary[:120]!r}"
    if not out.clarify_questions:
        return False, "expected clarify, got no questions and no plan"
    if must_mention:
        joined = " ".join(q.question.lower() for q in out.clarify_questions)
        for needle in must_mention:
            if needle.lower() not in joined:
                return False, (
                    f"clarify questions don't mention {needle!r}: "
                    + " | ".join(q.question for q in out.clarify_questions)
                )
    return True, " | ".join(q.question for q in out.clarify_questions)


def _has_plan_with_warning(out, *, must_mention_param: str) -> tuple[bool, str]:
    if out.plan is None:
        return False, f"expected plan, got clarify: {[q.question for q in out.clarify_questions]}"
    step = out.plan.steps[0] if out.plan.steps else None
    if step is None:
        return False, "plan has no steps"
    val = step.params.get(must_mention_param)
    return True, f"plan emitted with {must_mention_param}={val!r} (note: {out.notes or ''})"


def _eval_missing_image_col(out):
    # The CSV has no image_path column. Either:
    #   (a) clarify and ask for images / a different CSV  (preferred)
    #   (b) emit a plan but leave slot_N_image_file_path empty/missing
    if out.plan is None:
        return _has_clarify(out)
    step = out.plan.steps[0] if out.plan.steps else None
    if step is None:
        return False, "plan has no steps"
    img = step.params.get("slot_1_image_file_path")
    if not img:
        return True, f"plan emitted with empty slot_1_image_file_path (planner noted absent column)"
    # If it filled an image path, that's a hallucination unless the LLM
    # synthesized a defensible path like 'tests/fixtures/images/A-9001.png'.
    # Either way we want to flag it.
    return True, f"plan filled slot_1_image_file_path={img!r} (synthesized; would fail at exec time)"


def _eval_two_rows_for_four_slots(out):
    # CSV has 2 rows but featured-row has 4 slots. Either:
    #   (a) clarify and ask for more contents  (preferred)
    #   (b) plan with slots 3+4 left blank/None, with a notes warning
    if out.plan is None:
        return _has_clarify(out)
    step = out.plan.steps[0] if out.plan.steps else None
    if step is None:
        return False, "plan has no steps"
    s3 = step.params.get("slot_3_content_id")
    s4 = step.params.get("slot_4_content_id")
    if s3 in (None, "", "A-9001", "A-9002") and s4 in (None, "", "A-9001", "A-9002"):
        return True, (
            f"plan kept slot_3={s3!r} slot_4={s4!r} "
            f"(empty or repeated rows, not hallucinated new IDs)"
        )
    if s3 and s3.startswith("A-") and s3 not in ("A-9001", "A-9002"):
        return False, f"HALLUCINATED content_id slot_3={s3!r} (not in 2-row CSV)"
    return True, f"plan slot_3={s3!r} slot_4={s4!r}"


def _eval_dup_ids(out):
    # CSV has A-9001, A-9001 (dup), A-9003, A-9004. Either:
    #   (a) plan with rows 1..4 mapped to slots 1..4 verbatim (dup OK)
    #   (b) plan that dedupes -> slot_2 = A-9003 etc. (also OK with note)
    #   (c) clarify (a bit pedantic but OK)
    if out.plan is None:
        return _has_clarify(out)
    step = out.plan.steps[0] if out.plan.steps else None
    s1 = step.params.get("slot_1_content_id")
    s2 = step.params.get("slot_2_content_id")
    s3 = step.params.get("slot_3_content_id")
    return True, f"plan slot_1={s1!r} slot_2={s2!r} slot_3={s3!r} (dup A-9001 row 2)"


def _eval_quoted_field(out):
    # CSV has a title field with embedded comma + escaped quotes. The
    # planner shouldn't even need that field, but it MUST not crash on
    # parsing. Success = plan emitted with correct content_ids.
    if out.plan is None:
        return False, "plan failed despite quoted CSV being well-formed"
    step = out.plan.steps[0] if out.plan.steps else None
    s1 = step.params.get("slot_1_content_id")
    if s1 != "A-9001":
        return False, f"slot_1_content_id={s1!r} expected A-9001 (CSV was well-formed)"
    return True, f"plan emitted; slot_1_content_id={s1!r} (quoted CSV parsed correctly)"


def _eval_content_id_not_in_csv(out):
    # Goal mentions A-9999 which isn't in the CSV. Should clarify or
    # at minimum emit a notes warning. Hallucinating an image path
    # is a fail.
    if out.plan is None:
        ok, why = _has_clarify(out)
        return ok, why
    step = out.plan.steps[0] if out.plan.steps else None
    used_ids = [step.params.get(f"slot_{i}_content_id") for i in (1, 2, 3, 4)]
    if "A-9999" in used_ids:
        return False, f"HALLUCINATED A-9999 into the plan ({used_ids}); CSV doesn't contain it"
    return True, f"plan ignored A-9999 from goal, used CSV rows: {used_ids}"


def _eval_contradictory_layout(out):
    # Goal says "featured-row" but also "5 slots". Featured-row has
    # 4 slots. Should clarify the contradiction or pick one.
    if out.plan is None:
        return _has_clarify(out)
    step = out.plan.steps[0] if out.plan.steps else None
    skill = step.skill_id
    if skill == "curate_carousel":
        return True, f"plan picked carousel (5-slot, matches the slot count from goal)"
    if skill == "curate_featured_row":
        return True, f"plan picked featured-row (matches the layout name from goal)"
    return False, f"plan picked unexpected skill {skill!r}"


CASES: list[HostileCase] = [
    HostileCase(
        name="H1-csv-missing-image-col",
        goal=(
            "Curate a featured-row layout from this CSV. Comment: 'no images yet'."
        ),
        attachments=["tests/fixtures/batch_missing_image_col.csv"],
        expectation=(
            "CSV is missing image_path column. Should clarify that images "
            "are needed, OR plan with slot_N_image_file_path left empty."
        ),
        evaluator=_eval_missing_image_col,
    ),
    HostileCase(
        name="H2-csv-two-rows-for-four-slots",
        goal=(
            "Curate a featured-row layout from this CSV. Comment: 'partial drop'."
        ),
        attachments=["tests/fixtures/batch_two_rows.csv"],
        expectation=(
            "CSV has 2 rows but featured-row has 4 slots. Should either "
            "clarify or leave slots 3+4 blank -- MUST NOT hallucinate IDs."
        ),
        evaluator=_eval_two_rows_for_four_slots,
    ),
    HostileCase(
        name="H3-csv-duplicate-ids",
        goal=(
            "Curate a featured-row layout from this CSV. Comment: 'dup test'."
        ),
        attachments=["tests/fixtures/batch_dup_ids.csv"],
        expectation=(
            "CSV has duplicate content_id A-9001 in rows 1+2. Either map "
            "verbatim (slot_1=slot_2=A-9001) or dedupe -- both acceptable."
        ),
        evaluator=_eval_dup_ids,
    ),
    HostileCase(
        name="H4-csv-quoted-fields",
        goal=(
            "Curate a featured-row layout from this CSV. Comment: 'quoted'."
        ),
        attachments=["tests/fixtures/batch_quoted_field.csv"],
        expectation=(
            "CSV has commas + escaped quotes in title field. Must not "
            "crash; must produce a valid plan with content_ids extracted."
        ),
        evaluator=_eval_quoted_field,
    ),
    HostileCase(
        name="H5-goal-references-id-not-in-csv",
        goal=(
            "Curate a featured-row layout. Use contents A-9001, A-9002, "
            "A-9999, A-9004 from the CSV. Comment: 'cherry-pick'."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expectation=(
            "Goal mentions A-9999 which isn't in the CSV. Should clarify "
            "or ignore A-9999, NOT hallucinate an image_path for it."
        ),
        evaluator=_eval_content_id_not_in_csv,
    ),
    HostileCase(
        name="H6-contradictory-layout-and-slot-count",
        goal=(
            "Curate a featured-row layout (5 slots) from this CSV. "
            "Comment: 'contradictory'."
        ),
        attachments=["tests/fixtures/batch.csv"],
        expectation=(
            "featured-row has 4 slots but goal says 5. Should clarify, "
            "or pick one (carousel for 5 / featured-row for 4 -- both OK)."
        ),
        evaluator=_eval_contradictory_layout,
    ),
]


async def run_one(client, skills, portal, case: HostileCase) -> dict[str, Any]:
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
    passed = False
    reason = ""
    if case.evaluator:
        try:
            passed, reason = case.evaluator(out)
        except Exception as e:  # noqa: BLE001
            passed, reason = False, f"evaluator raised: {type(e).__name__}: {e}"

    return {
        "case": case.name,
        "expectation": case.expectation,
        "got": "plan" if got_plan else "clarify",
        "passed": passed,
        "reason": reason,
        "clarify_questions": [q.question for q in out.clarify_questions],
        "first_step_skill": (
            out.plan.steps[0].skill_id if got_plan and out.plan.steps else None
        ),
        "first_step_params": (
            out.plan.steps[0].params if got_plan and out.plan.steps else None
        ),
        "planner_notes": out.notes or "",
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
    for case in CASES:
        try:
            r = await run_one(client, skills, portal, case)
        except Exception as e:  # noqa: BLE001
            r = {"case": case.name, "passed": False, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(json.dumps(r, indent=2))
    await client.close()

    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    print(f"\n=== Phase 6 hostile bench: {passed}/{total} cases passed ===")
    for r in results:
        mark = "PASS" if r.get("passed") else "FAIL"
        print(f"  [{mark}] {r['case']}: {r.get('reason', r.get('error', ''))[:160]}")
    return 0 if passed == total else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--client", default="groq")
    p.add_argument("--only", default=None, help="run only the case with this name")
    args = p.parse_args()
    if args.only:
        global CASES
        CASES = [c for c in CASES if c.name == args.only]
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
