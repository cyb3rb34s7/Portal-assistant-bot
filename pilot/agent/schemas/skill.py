"""Intelligent skill schema (v2).

Adds metadata on top of the v1 recorded-trace skill format:
  - description, semantic param names, source hints
  - preconditions
  - success_assertions
  - destructive_actions
  - schema_version

Backwards-compatible: a v1 skill (no `schema_version` or set to 1) is
accepted by ``load_skill`` and adapted via ``upgrade_v1_to_v2`` with
empty metadata fields. The annotate LLM pass populates them later.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


SKILL_SCHEMA_VERSION = 2


class SkillParameter(BaseModel):
    name: str = Field(description="Internal parameter name (snake_case).")
    semantic: str | None = Field(
        default=None,
        description="Human-readable description ('Asset identifier, e.g. A-9001').",
    )
    required: bool = True
    source_hint: str | None = Field(
        default=None,
        description="Where the planner should look for this value "
        "(e.g. 'PPT slide table column Asset ID' or 'user-provided').",
    )
    default_hint: str | None = None
    # WI-05: typed param expansion. The v2 sidecar shape mirrors the v1
    # SkillParam.type Literal so a planner/LLM speaking the v2 schema
    # can produce values that survive runner-side codec resolution.
    type: Literal[
        "string", "number", "boolean", "date", "datetime",
        "file_path", "enum", "number_range", "string_list", "object",
    ] = "string"
    label_options: list[str] | None = Field(
        default=None,
        description=(
            "id+label sprint: for multi-select (string_list) params, the "
            "human option LABELS seen at record time (from the "
            "set_selection spec's known_options). Replay values are these "
            "LABELS -- the runner resolves each label to its id "
            "internally. A planner clarify step consumes this list to "
            "offer the operator the seen options; the interactive UX is "
            "not built here, only the data is surfaced. None for "
            "non-multiselect params."
        ),
    )


class SuccessAssertion(BaseModel):
    type: Literal[
        "text_visible", "url_matches", "element_visible", "element_count"
    ]
    text: str | None = None
    pattern: str | None = None
    selector: str | None = None
    scope: str | None = Field(
        default=None,
        description="Optional area to scope the assertion ('toast', 'modal', 'page').",
    )
    count_min: int | None = None
    count_max: int | None = None


class DestructiveActionSpec(BaseModel):
    step: int
    kind: str = Field(description="publish | delete | archive | save | ...")
    reversible: bool = False
    confirm_prompt: str | None = None


class StructuralOverlay(BaseModel):
    """WI-31: per-step LLM-enriched structural metadata.

    The deterministic annotator (annotate.py) produces the structural
    truth (depends_on, expected_signals, set_selection clusters,
    etc.). The LLM enrichment pass runs SECOND and can only ADD
    advisory metadata that the runner / planner can use, never
    contradict the deterministic decisions.

    Each overlay is validated against the raw trace evidence at apply
    time: a depends_on claim must have a supporting causal event in
    the trace; an expected_signal must reference a captured URL
    pattern. Hallucinated overlays are dropped with a
    ``llm_overlay_rejected`` audit entry rather than being silently
    persisted.

    Fields are independent and optional -- the LLM populates the ones
    it can support with evidence from the trace; unsupported claims
    are omitted rather than fabricated.
    """

    step_index: int = Field(
        description="Index of the step this overlay applies to."
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "Other step indices (as strings) whose effect this step "
            "needs before running. Each entry must have a corresponding "
            "causal trace event; unbacked entries are rejected at "
            "validation."
        ),
    )
    expected_signals: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Network or DOM signals the step is expected to produce. "
            "Each entry is {kind: 'network'|'dom', url_pattern?: str, "
            "selector?: str}. Each claim must be backed by a recorded "
            "network_request / dom_mutation event."
        ),
    )
    assert_after: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Post-action assertions (text_visible, url_matches, ...). "
            "LLM-suggested; runner gates persistence on observed "
            "verification."
        ),
    )
    widget_type: str | None = Field(
        default=None,
        description=(
            "LLM's best-guess widget label (e.g. 'cascading_select', "
            "'autocomplete', 'multiselect_with_search'). Advisory; the "
            "deterministic cluster_kind already drives the runner."
        ),
    )
    risk_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Plain-English notes about replay risks the LLM noticed "
            "(e.g. 'this select cascades; recorded value may not "
            "exist for a different parent'). Audit/UI only."
        ),
    )
    param_alias_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-step param alias map (semantic_name -> v1 binding "
            "name). Extends the top-level param_alias_map for cases "
            "where the same v1 binding is renamed differently per "
            "step context (rare; usually empty)."
        ),
    )


class SkillStepRef(BaseModel):
    """Lightweight reference to a step in the recorded trace.

    The full step payload remains in the existing trace.jsonl format;
    this is just enough metadata for planning.
    """

    idx: int
    kind: str
    test_id: str | None = None
    note: str | None = None


class SkillFile(BaseModel):
    schema_version: int = SKILL_SCHEMA_VERSION

    id: str
    name: str
    description: str | None = Field(
        default=None,
        description="One-paragraph human-readable summary of what the skill does.",
    )
    version: int = 1

    parameters: list[SkillParameter] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    success_assertions: list[SuccessAssertion] = Field(default_factory=list)
    destructive_actions: list[DestructiveActionSpec] = Field(default_factory=list)

    # base_url for the portal this skill targets. Used by the executor as
    # a default `target_url_substring` when attaching to the operator's
    # Chrome (so the right tab is picked when several are open).
    base_url: str | None = None

    # Maps semantic parameter name (planner-emitted) -> v1 trace binding
    # name (recorded fingerprint). Populated from `<skill>.v2.json`
    # sidecars by load_skill_library; the executor reads it to translate
    # the planner's params before handing them to SkillRunner. Stored on
    # the skill so it travels with the model — no module-level globals.
    param_alias_map: dict[str, str] = Field(default_factory=dict)

    # WI-31: per-step structural overlays from the LLM enrichment pass.
    # Each entry must reference a step_index that exists in steps. The
    # runner / planner reads these AFTER the deterministic annotator's
    # decisions; overlays can only ADD advisory metadata, never
    # contradict structural truth.
    structural_overlays: list[StructuralOverlay] = Field(
        default_factory=list,
        description=(
            "Per-step LLM-enriched structural metadata. Validated "
            "against raw trace evidence at apply time; unsupported "
            "claims are dropped."
        ),
    )

    # Recorded steps (untyped here; the runner uses the existing
    # skill_models.py types). We keep them as a raw list so legacy v1
    # skills still load.
    steps: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: int) -> int:
        if v not in (1, 2):
            raise ValueError(f"unsupported skill schema_version {v}")
        return v


_V1_TYPE_MAP = {"string": "string", "number": "number", "boolean": "boolean",
                "date": "date", "datetime": "datetime",
                "int": "number", "integer": "number",
                "float": "number", "bool": "boolean",
                "file_path": "file_path", "filepath": "file_path",
                "path": "file_path",
                # WI-05: typed param expansion. v1 SkillParam type
                # Literal now includes enum/number_range/string_list/
                # object, and the v2 sidecar accepts them so a v1->v2
                # migration of a typed skill keeps the type intact.
                "enum": "enum",
                "number_range": "number_range",
                "string_list": "string_list",
                "object": "object"}


def _migrate_v1_params(legacy_params: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """v1 used a top-level field name `params` whose entries had keys
    {name, type, description, example, required}; v2 uses `parameters`
    whose entries have keys {name, semantic, required, type,
    source_hint, default_hint}. Rename + reshape, mapping v1 ``example``
    -> v2 ``default_hint`` so the example value is preserved for
    skills that have not yet been re-annotated by the LLM pass."""
    out: list[dict[str, Any]] = []
    for p in legacy_params or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not name:
            continue
        example = p.get("example")
        out.append({
            "name": name,
            "semantic": p.get("description") or None,
            "required": bool(p.get("required", True)),
            "type": _V1_TYPE_MAP.get(str(p.get("type", "string")).lower(), "string"),
            "source_hint": None,
            "default_hint": str(example) if example not in (None, "") else None,
        })
    return out


def upgrade_v1_to_v2(legacy: dict[str, Any]) -> SkillFile:
    """Coerce a legacy v1 skill dict into v2 with empty metadata.

    The most important thing this does is rename the v1 top-level
    `params` field into v2 `parameters` and reshape each entry; without
    this, existing skills load with an empty `parameters` list and the
    planner strips every parameter as 'unknown' (that bug burned us
    once already — see DOCS/CONTEXT.md P-006).

    Used to keep the existing ``skills/curate_one_item.json`` file
    loadable until an annotate LLM pass enriches it with semantic +
    source_hint metadata.
    """
    data = dict(legacy)
    data.setdefault("schema_version", 2)

    # Synthesize id from filename if missing — v1 didn't have an `id`.
    if "id" not in data:
        # Best-effort: use `name` slugified.
        n = str(data.get("name", "skill"))
        data["id"] = n.replace(" ", "_").lower()

    # Migrate `params` -> `parameters` if v2 field is absent.
    if "parameters" not in data and "params" in data:
        data["parameters"] = _migrate_v1_params(data["params"])

    # v1 skills carry `base_url` at the top level too; pass it through.
    # Also drop legacy fields the v2 model doesn't know about (the model
    # rejects extras silently otherwise).
    return SkillFile.model_validate(data)
