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
    type: Literal["string", "number", "boolean", "date", "file_path"] = "string"


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
                "date": "date", "int": "number", "integer": "number",
                "float": "number", "bool": "boolean",
                "file_path": "file_path", "filepath": "file_path",
                "path": "file_path"}


def _migrate_v1_params(legacy_params: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """v1 used a top-level field name `params` whose entries had keys
    {name, type, description, example, required}; v2 uses `parameters`
    whose entries have keys {name, semantic, required, type,
    source_hint, default_hint}. Rename + reshape with conservative
    defaults; we drop `example` (it isn't part of v2; the annotate
    pass will add `default_hint` properly later)."""
    out: list[dict[str, Any]] = []
    for p in legacy_params or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not name:
            continue
        out.append({
            "name": name,
            "semantic": p.get("description") or None,
            "required": bool(p.get("required", True)),
            "type": _V1_TYPE_MAP.get(str(p.get("type", "string")).lower(), "string"),
            "source_hint": None,
            "default_hint": p.get("example") if p.get("example") is not None else None,
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

    return SkillFile.model_validate(data)
