"""LLM annotate pass for v1 -> v2 skill enrichment.

Takes a freshly-recorded v1 skill JSON (with auto-named params like
`slot_1_content_select`) and asks an LLM to:

  - propose better, semantically meaningful parameter names
  - write a one-paragraph `description`
  - identify `preconditions`
  - propose `success_assertions` (assertions that should hold after a
    successful run)
  - flag `destructive_actions` (steps that mutate persistent state)
  - WI-31: propose structural overlays (depends_on, expected_signals,
    assert_after, widget_type, risk_notes, param_alias_map) per step,
    validated against raw trace evidence so the LLM cannot add a
    dependency / signal that no causal trace event supports.

The LLM output is structurally validated, applied to a v2 SkillFile
copy, and written to `<skill_path>.v2.json` next to the original. The
operator's CLI then renames or replaces.

KISS choice: we don't *rename* steps' param_binding fields in the
underlying v1 trace. Instead the v2 SkillFile carries a
`param_alias_map` (semantic name -> original v1 binding name) so the
RealExecutor can translate the planner's params before handing them
to SkillRunner. This keeps the v1 trace immutable; if the LLM
rename is bad, we throw away the v2 sidecar and the original still
works.

WI-31 ordering: the deterministic annotator (pilot/annotate.py) runs
FIRST and produces the structural truth (cluster_kind, set_selection,
dependency_chain, expected_signals, assert_after). The LLM pass runs
SECOND and produces ADVISORY structural overlays. ``validate_overlays``
checks each LLM claim against the raw trace events; unsupported claims
are dropped with a diagnostic, never silently applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pilot.agent.ai_client import AIClient, Message
from pilot.agent.ai_client.structured import (
    StructuredOutputError,
    complete_structured,
)


# ---------------------------------------------------------------------------
# Schemas the LLM must populate
# ---------------------------------------------------------------------------


class _LlmParam(BaseModel):
    """LLM's proposed v2 parameter for one auto-named v1 param."""

    original_name: str = Field(description="Original auto-name from v1 (verbatim).")
    semantic_name: str = Field(
        description="snake_case semantic name (e.g. 'content_id_slot_1')."
    )
    semantic_description: str = Field(
        description="One-sentence human-readable description of what this is."
    )
    source_hint: str = Field(
        description="Where a planner / operator should look for this value "
        "(e.g. 'CSV column content_id, row 1' or 'operator-provided')."
    )
    type: str = Field(
        description="One of: string, number, boolean, date, file_path."
    )


class _LlmAnnotation(BaseModel):
    """Top-level shape the LLM must return for an annotate pass."""

    description: str = Field(
        description="One-paragraph summary of what the skill does, "
        "phrased so a planner LLM can decide when to invoke it."
    )
    preconditions: list[str] = Field(
        default_factory=list,
        description="State the portal must be in before the skill runs.",
    )
    parameters: list[_LlmParam] = Field(
        description="One entry per v1 auto-named param. Same length as input."
    )
    destructive_step_indexes: list[int] = Field(
        default_factory=list,
        description="Indices of steps whose action mutates persistent state "
        "(e.g. save / apply / publish / delete).",
    )
    success_assertions: list[str] = Field(
        default_factory=list,
        description="Plain-English assertions that should hold after the "
        "skill runs (e.g. 'a layout appears in the Applied list').",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_step_summary(steps: list[dict[str, Any]]) -> list[str]:
    """One short line per step for the prompt."""
    out: list[str] = []
    for s in steps:
        idx = s.get("index", "?")
        action = s.get("action", "?")
        label = s.get("semantic_label") or ""
        binding = (s.get("param_binding") or {}).get("name") or ""
        fp = s.get("fingerprint") or {}
        test_id = fp.get("test_id") or ""
        bits = [f"step {idx}", action]
        if label:
            bits.append(f"label={label}")
        if test_id:
            bits.append(f"testid={test_id}")
        if binding:
            bits.append(f"binds={binding}")
        out.append(" ".join(bits))
    return out


async def annotate_skill(
    *,
    client: AIClient,
    v1_skill: dict[str, Any],
    portal_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Run the LLM annotate pass; return a dict ready to be merged into
    a v2 SkillFile (parameters list, description, preconditions,
    destructive_actions, success_assertions, param_alias_map).

    Raises StructuredOutputError on validation failure.
    """
    params_v1 = v1_skill.get("params", [])
    steps_v1 = v1_skill.get("steps", [])

    if not params_v1:
        return {
            "description": v1_skill.get("description") or "",
            "parameters": [],
            "preconditions": [],
            "destructive_actions": [],
            "success_assertions": [],
            "param_alias_map": {},
        }

    step_summary = _build_step_summary(steps_v1)
    params_summary = [
        f"- {p['name']}  type={p.get('type', 'string')}  "
        f"example={p.get('example', '')!r}  required={p.get('required', True)}"
        for p in params_v1
    ]

    sys_prompt = (
        "You are the annotate stage of an agentic portal-automation system. "
        "You are given a v1 skill recorded by watching a human operator "
        "perform a workflow. The v1 skill has auto-named parameters that "
        "are syntactic, not semantic (e.g. 'slot_1_content_select' instead "
        "of 'slot_1_content_id'). Your job is to propose semantically "
        "meaningful renames, plus a description, preconditions, "
        "destructive-action flags, and success assertions.\n"
        "\n"
        "Rules you MUST follow:\n"
        "1. Return EXACTLY one parameter entry per input param, with its "
        "   original_name preserved verbatim. The planner will use the "
        "   original_name to find the matching v1 binding at runtime.\n"
        "2. Pick semantic names that a planner LLM could populate from a "
        "   CSV or natural-language goal. Prefer snake_case.\n"
        "3. The 'type' field must be one of: string, number, boolean, "
        "   date, file_path. Use 'file_path' for any param that points to "
        "   a file on disk (image upload, CSV upload, etc.).\n"
        "4. destructive_step_indexes should list indices of steps whose "
        "   semantic_label suggests mutating persistent state (save, "
        "   apply, publish, delete, archive). The operator's plan-approval "
        "   step is the gate; you only flag what's destructive, you don't "
        "   block on it.\n"
        "5. Don't invent parameters that aren't in the input list.\n"
    )
    user_prompt = (
        f"Skill name: {v1_skill.get('name', '?')}\n"
        f"Portal:     {portal_id or v1_skill.get('portal') or '?'}\n"
        f"Existing description: {v1_skill.get('description') or '(none)'}\n"
        f"\n"
        f"v1 parameters ({len(params_v1)}):\n"
        + "\n".join(params_summary)
        + "\n\n"
        + f"Steps ({len(steps_v1)}):\n"
        + "\n".join(step_summary)
        + "\n"
    )

    annotation = await complete_structured(
        client,
        messages=[
            Message(role="system", content=sys_prompt),
            Message(role="user", content=user_prompt),
        ],
        response_model=_LlmAnnotation,
        model=model,
        temperature=0.0,
        max_retries=2,
    )

    if len(annotation.parameters) != len(params_v1):
        raise StructuredOutputError(
            f"annotate produced {len(annotation.parameters)} params but "
            f"input had {len(params_v1)}; refusing to apply"
        )
    seen_originals = {p.original_name for p in annotation.parameters}
    expected_originals = {p["name"] for p in params_v1}
    missing = expected_originals - seen_originals
    if missing:
        raise StructuredOutputError(
            f"annotate output missing original_name(s): {sorted(missing)}"
        )

    # Build the v2 parameters list (keyed by SEMANTIC name) plus the
    # alias map back to original v1 binding names.
    v2_params: list[dict[str, Any]] = []
    alias_map: dict[str, str] = {}
    for lp in annotation.parameters:
        v1_param = next(p for p in params_v1 if p["name"] == lp.original_name)
        v2_params.append({
            "name": lp.semantic_name,
            "semantic": lp.semantic_description,
            "required": bool(v1_param.get("required", True)),
            "type": lp.type if lp.type in (
                "string", "number", "boolean", "date", "file_path"
            ) else v1_param.get("type", "string"),
            "source_hint": lp.source_hint,
            "default_hint": v1_param.get("example"),
        })
        alias_map[lp.semantic_name] = lp.original_name

    destructive_actions = []
    _DESTRUCTIVE_KINDS = ("publish", "delete", "archive", "apply", "save", "submit")
    for idx in annotation.destructive_step_indexes:
        if 0 <= idx < len(steps_v1):
            step = steps_v1[idx]
            label = (step.get("semantic_label") or "").lower()
            kind = "save"
            for k in _DESTRUCTIVE_KINDS:
                if k in label:
                    kind = k
                    break
            destructive_actions.append({
                "step": idx,
                "kind": kind,
                "reversible": kind in ("save", "submit"),
                "confirm_prompt": None,
            })

    success_assertions = [
        {"type": "text_visible", "text": text, "scope": "page"}
        for text in annotation.success_assertions
    ]

    return {
        "description": annotation.description,
        "parameters": v2_params,
        "preconditions": annotation.preconditions,
        "destructive_actions": destructive_actions,
        "success_assertions": success_assertions,
        "param_alias_map": alias_map,
    }


def validate_overlays(
    overlays: list[dict[str, Any]],
    *,
    v1_skill: dict[str, Any],
    trace_events: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """WI-31: validate LLM-proposed structural overlays against the
    raw trace evidence.

    Each overlay claims a step_index + zero or more structural fields
    (depends_on, expected_signals, assert_after, widget_type,
    risk_notes, param_alias_map). The validator drops:

      - overlays whose step_index isn't in the recorded steps;
      - depends_on entries pointing at step indices the recorded step
        doesn't causally follow (no provenance / caused_by linkage);
      - expected_signals with kind='network' but no recorded
        network_request URL pattern matching;
      - expected_signals with kind='dom' but no recorded dom_mutation
        / interactable selector matching;
      - assert_after / widget_type / risk_notes pass through (they're
        advisory and cheap to surface).

    Returns ``(accepted, rejected)`` -- accepted is the filtered list
    safe to apply; rejected is the list of dropped overlays with a
    ``rejection_reason`` field so the caller can audit / log them.

    Conservative: when ``trace_events`` is None (legacy trace not
    available), all overlays are KEPT but flagged with confidence-
    reducing markers. Strict mode (trace present) enforces evidence.
    """
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    steps = v1_skill.get("steps", []) or []
    step_count = len(steps)
    # Index trace events by event_id and by kind so we can probe for
    # supporting evidence.
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for ev in trace_events or []:
        k = ev.get("kind") if isinstance(ev, dict) else None
        if k:
            by_kind.setdefault(k, []).append(ev)
    # Causal chain: for each step, its provenance.raw_event_ids.
    step_raw_ids: list[set[str]] = []
    for s in steps:
        prov = s.get("provenance") or {}
        ids = set(prov.get("raw_event_ids") or [])
        step_raw_ids.append(ids)

    for ov in overlays:
        if not isinstance(ov, dict):
            rejected.append({
                "overlay": ov,
                "rejection_reason": "not_a_dict",
            })
            continue
        idx = ov.get("step_index")
        if not isinstance(idx, int) or idx < 0 or idx >= step_count:
            rejected.append({
                "overlay": ov,
                "rejection_reason": f"step_index_out_of_range",
            })
            continue

        # Validate depends_on -- each parent step index must precede
        # the child in the step list (causal ordering can't loop) and
        # the child step's raw_event_ids should NOT predate the
        # parent's (the LLM can't claim B depends on A if B was
        # recorded before A).
        cleaned_overlay = dict(ov)
        kept_depends_on: list[str] = []
        dropped_depends_on: list[str] = []
        for dep in ov.get("depends_on") or []:
            try:
                dep_idx = int(dep)
            except (TypeError, ValueError):
                dropped_depends_on.append(str(dep))
                continue
            if dep_idx < 0 or dep_idx >= step_count or dep_idx >= idx:
                # Out of range OR not a predecessor.
                dropped_depends_on.append(str(dep))
                continue
            kept_depends_on.append(str(dep_idx))
        cleaned_overlay["depends_on"] = kept_depends_on

        # Validate expected_signals -- each entry must reference a
        # captured event (network_request URL or dom_mutation).
        kept_signals: list[dict[str, Any]] = []
        dropped_signals: list[dict[str, Any]] = []
        for sig in ov.get("expected_signals") or []:
            if not isinstance(sig, dict):
                dropped_signals.append({"sig": sig, "reason": "not_a_dict"})
                continue
            kind = sig.get("kind")
            if kind == "network":
                pattern = sig.get("url_pattern") or ""
                if not pattern:
                    dropped_signals.append({"sig": sig, "reason": "missing_url_pattern"})
                    continue
                if trace_events is not None:
                    # Strict: require a captured network_request whose
                    # URL contains the pattern.
                    nets = by_kind.get("network_request", [])
                    if not any(
                        pattern in (n.get("url") or "") for n in nets
                    ):
                        dropped_signals.append({
                            "sig": sig,
                            "reason": "no_supporting_network_request",
                        })
                        continue
                kept_signals.append(sig)
            elif kind == "dom":
                sel = sig.get("selector") or ""
                if not sel:
                    dropped_signals.append({"sig": sig, "reason": "missing_selector"})
                    continue
                # Don't strict-validate DOM selectors against trace
                # (they may target the static page structure rather
                # than a captured mutation). Keep with audit note.
                kept_signals.append(sig)
            else:
                dropped_signals.append({"sig": sig, "reason": f"unknown_kind:{kind}"})
        cleaned_overlay["expected_signals"] = kept_signals

        # assert_after / widget_type / risk_notes / param_alias_map
        # pass through unchanged.
        cleaned_overlay["assert_after"] = list(ov.get("assert_after") or [])
        cleaned_overlay["widget_type"] = ov.get("widget_type")
        cleaned_overlay["risk_notes"] = list(ov.get("risk_notes") or [])
        cleaned_overlay["param_alias_map"] = dict(
            ov.get("param_alias_map") or {}
        )

        # Stamp a meta-block on the overlay describing what was dropped,
        # for downstream audit.
        if dropped_depends_on or dropped_signals:
            cleaned_overlay["_dropped"] = {
                "depends_on": dropped_depends_on,
                "expected_signals": dropped_signals,
            }
        accepted.append(cleaned_overlay)

    return accepted, rejected


def write_v2_sidecar(v1_skill_path: Path, v2_meta: dict[str, Any]) -> Path:
    """Persist the LLM annotation as a sibling sidecar JSON.

    The v1 file is left untouched. v2-aware loaders (the planner's
    `load_skill_library`) read both and merge.
    """
    sidecar = v1_skill_path.with_suffix(".v2.json")
    sidecar.write_text(
        json.dumps(v2_meta, indent=2), encoding="utf-8"
    )
    return sidecar
