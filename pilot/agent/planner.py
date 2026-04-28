"""Planner stage.

Given (goal, IntakeEntities, skill library, portal context), produce
either:
  - a Plan (with steps + destructive_actions surfaced from skills), or
  - a list of ClarifyQuestion items (when the input is ambiguous).

The planner uses the LLM to decide WHICH skills to invoke and HOW to
populate each parameter. The deterministic skill_runner then executes
the result; the planner does NOT reason about clicks or selectors.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from pilot.agent.ai_client import AIClient, Message, complete_structured
from pilot.agent.ai_client.structured import StructuredOutputError
from pilot.agent.schemas.domain import (
    DestructiveAction,
    IntakeEntities,
    Plan,
    PlanStep,
    SkillInvocationSummary,
)
from pilot.agent.schemas.portal_context import PortalContext, render_for_prompt
from pilot.agent.schemas.protocol import ClarifyOption, ClarifyQuestion
from pilot.agent.schemas.skill import SkillFile, upgrade_v1_to_v2


# ---------------------------------------------------------------------------
# Skill library loader
# ---------------------------------------------------------------------------


def load_skill_library(skills_dir: Path) -> list[SkillFile]:
    """Load all .json/.yaml skill files from a directory.

    v1 skills (no schema_version) are upgraded in-memory via
    `upgrade_v1_to_v2`. Files that fail validation are skipped with a
    warning print to stderr (the orchestrator emits this as
    agent.log{level:'warn'} downstream).
    """
    if not skills_dir.exists():
        return []

    out: list[SkillFile] = []
    for p in sorted(skills_dir.iterdir()):
        if p.suffix.lower() not in (".json", ".yaml", ".yml"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
            if p.suffix.lower() == ".json":
                data = json.loads(text)
            else:
                import yaml

                data = yaml.safe_load(text)
            if not isinstance(data, dict):
                continue
            # Inject id/name from filename if absent (v1 skills often
            # didn't have an id field).
            if "id" not in data:
                data["id"] = p.stem
            if "name" not in data:
                data["name"] = data["id"]
            skill = upgrade_v1_to_v2(data)
            out.append(skill)
        except Exception as e:  # noqa: BLE001 - intentional broad catch
            print(f"[planner] skill load failed for {p.name}: {e}", flush=True)
    return out


# ---------------------------------------------------------------------------
# LLM contract
# ---------------------------------------------------------------------------


class _PlanCandidateStep(BaseModel):
    skill_id: str
    params: dict[str, Any]
    param_sources: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None


class _PlanOrClarify(BaseModel):
    """Discriminated union the LLM emits.

    `decision` controls which branch is populated. We pre-validate by
    pulling apart the model after parse rather than using a Union here
    so the JSON Schema we ship to the LLM is simpler.
    """

    decision: Literal["plan", "clarify"]
    plan_summary: str | None = None
    plan_steps: list[_PlanCandidateStep] = Field(default_factory=list)
    estimated_duration_seconds: int = 0
    clarify_questions: list[ClarifyQuestion] = Field(default_factory=list)
    notes: str | None = None


_PLANNER_SYSTEM_PROMPT = """\
You are the planner stage of a portal-automation assistant. Your job:
given an operator goal, structured intake entities, a portal context,
and a list of available skills, decide either:

  (a) propose a plan (decision="plan") that lists which skill to call
      with which parameters, in execution order; or
  (b) ask up to 5 clarification questions (decision="clarify") if the
      input is genuinely ambiguous.

Strict rules:
- Use only skills that appear in the catalog; do NOT invent skill ids.
- Use only parameter names that appear in the chosen skill's parameters.
- Every parameter you set must have a `param_sources` entry explaining
  where the value came from ("pptx asset id", "user-provided",
  "portal_context default", etc.).
- Do not ask redundant clarify questions; questions must each eliminate
  ambiguity that you could not resolve from the context alone.
- Prefer plan over clarify when the data supports a confident decision.
- For multi-item goals, emit one plan_step per item.
"""


def _summarise_skills(skills: list[SkillFile]) -> str:
    if not skills:
        return "(no skills available)"
    lines = []
    for s in skills:
        params = ", ".join(
            f"{p.name}({p.semantic or p.type}{'*' if p.required else ''})"
            for p in s.parameters
        )
        lines.append(f"- {s.id}: {s.description or s.name} -- params: {params}")
    return "\n".join(lines)


def _build_user_prompt(
    *,
    goal: str,
    entities: IntakeEntities,
    portal: PortalContext | None,
    skills: list[SkillFile],
) -> str:
    portal_block = (
        render_for_prompt(portal) if portal else "(no portal context provided)"
    )
    return f"""\
Operator goal:
{goal}

Portal context:
{portal_block}

Available skills:
{_summarise_skills(skills)}

Intake entities (already extracted from operator inputs):
{entities.model_dump_json(indent=2)}

Decide between proposing a plan or asking clarify questions, and
respond as the JSON schema requires.
"""


# ---------------------------------------------------------------------------
# Public planner entry
# ---------------------------------------------------------------------------


class PlannerOutput(BaseModel):
    """What the planner returns to the orchestrator."""

    plan: Plan | None = None
    clarify_questions: list[ClarifyQuestion] = Field(default_factory=list)
    notes: str | None = None


def _build_destructive_actions(
    plan_steps: list[PlanStep], skills_by_id: dict[str, SkillFile]
) -> list[DestructiveAction]:
    out: list[DestructiveAction] = []
    for step in plan_steps:
        skill = skills_by_id.get(step.skill_id)
        if not skill:
            continue
        for da in skill.destructive_actions:
            out.append(
                DestructiveAction(
                    step_idx=step.idx,
                    kind=da.kind,
                    reversible=da.reversible,
                    label=f"{da.kind} ({step.skill_id} step {step.idx})",
                )
            )
    return out


def _build_skill_summary(plan_steps: list[PlanStep], skills_by_id: dict[str, SkillFile]) -> list[SkillInvocationSummary]:
    counts: dict[str, int] = {}
    for step in plan_steps:
        counts[step.skill_id] = counts.get(step.skill_id, 0) + 1
    out: list[SkillInvocationSummary] = []
    for sid, n in counts.items():
        skill = skills_by_id.get(sid)
        out.append(
            SkillInvocationSummary(
                skill_id=sid,
                invocations=n,
                description=skill.description if skill else None,
            )
        )
    return out


def _validate_planner_output(
    candidate: _PlanOrClarify, skills: list[SkillFile]
) -> tuple[Plan | None, list[ClarifyQuestion], str | None]:
    """Translate the LLM output to (Plan|None, clarify_questions, notes)
    and validate skill ids and param names.
    """
    if candidate.decision == "clarify":
        # Cap question count at 5 (hard budget).
        return None, candidate.clarify_questions[:5], candidate.notes

    skills_by_id = {s.id: s for s in skills}
    plan_steps: list[PlanStep] = []
    rejected: list[str] = []

    for i, cs in enumerate(candidate.plan_steps, start=1):
        skill = skills_by_id.get(cs.skill_id)
        if skill is None:
            rejected.append(f"unknown skill_id {cs.skill_id!r}")
            continue
        valid_param_names = {p.name for p in skill.parameters}
        unknown_params = [k for k in cs.params if k not in valid_param_names]
        if unknown_params:
            rejected.append(
                f"step {i} skill {cs.skill_id} got unknown params {unknown_params}"
            )
            # We keep the step but strip unknown params, recording the
            # event in notes for transparency.
            cs.params = {k: v for k, v in cs.params.items() if k in valid_param_names}
        plan_steps.append(
            PlanStep(
                idx=i,
                skill_id=cs.skill_id,
                params=cs.params,
                param_sources=cs.param_sources,
                notes=cs.notes,
            )
        )

    if not plan_steps:
        # Planner asked for a plan but it was unusable. Convert to a
        # single fallback clarify question so the orchestrator can decide.
        q = ClarifyQuestion(
            id=f"q-{uuid.uuid4().hex[:6]}",
            question=(
                "I could not build a usable plan from your inputs. "
                "Could you describe the workflow more specifically?"
            ),
            options=[],
            allow_custom_answer=True,
            priority="high",
        )
        return None, [q], "; ".join(rejected) if rejected else candidate.notes

    plan = Plan(
        id=f"p-{uuid.uuid4().hex[:6]}",
        summary=candidate.plan_summary or f"Run {len(plan_steps)} step(s)",
        skill_summary=_build_skill_summary(plan_steps, skills_by_id),
        steps=plan_steps,
        destructive_actions=_build_destructive_actions(plan_steps, skills_by_id),
        estimated_duration_seconds=candidate.estimated_duration_seconds
        or len(plan_steps) * 30,
        preconditions=[],
    )
    notes = "; ".join(rejected) if rejected else candidate.notes
    return plan, [], notes


async def run_planner(
    *,
    client: AIClient,
    goal: str,
    entities: IntakeEntities,
    skills: list[SkillFile],
    portal: PortalContext | None,
    model: str | None = None,
) -> PlannerOutput:
    """Run the planner stage.

    Falls back to a clarify list if the LLM output is unusable.
    """
    if not skills:
        # No skills loaded -> can't plan. Surface as a high-priority
        # clarify so the host knows.
        return PlannerOutput(
            plan=None,
            clarify_questions=[
                ClarifyQuestion(
                    id=f"q-{uuid.uuid4().hex[:6]}",
                    question=(
                        "There are no skills installed for this portal. "
                        "Record one with `python -m pilot teach <name>` "
                        "before retrying."
                    ),
                    options=[],
                    allow_custom_answer=False,
                    priority="high",
                )
            ],
            notes="empty_skill_library",
        )

    try:
        candidate = await complete_structured(
            client,
            messages=[
                Message(role="system", content=_PLANNER_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=_build_user_prompt(
                        goal=goal,
                        entities=entities,
                        portal=portal,
                        skills=skills,
                    ),
                ),
            ],
            response_model=_PlanOrClarify,
            model=model,
            temperature=0.0,
            max_retries=2,
        )
    except StructuredOutputError as e:
        return PlannerOutput(
            plan=None,
            clarify_questions=[
                ClarifyQuestion(
                    id=f"q-{uuid.uuid4().hex[:6]}",
                    question=(
                        "I had trouble understanding your request. Can you "
                        "describe the goal more concretely?"
                    ),
                    options=[],
                    allow_custom_answer=True,
                    priority="high",
                )
            ],
            notes=f"structured_output_error: {e}",
        )

    plan, clarify, notes = _validate_planner_output(candidate, skills)
    return PlannerOutput(plan=plan, clarify_questions=clarify, notes=notes)
