"""In-process domain types for agent stages.

These are the typed values intake/planner/clarify/orchestrator pass
between each other. Distinct from `protocol.py`, which is the on-the-
wire format.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Intake stage outputs
# ---------------------------------------------------------------------------


class ContentItem(BaseModel):
    """One item the operator wants the agent to act on."""

    id: str = Field(description="Asset identifier, e.g. 'A-9001'.")
    title: str | None = Field(default=None)
    thumbnail_path: str | None = Field(
        default=None, description="Resolved local path to a thumbnail file."
    )
    raw_source: str | None = Field(
        default=None, description="Where this came from (e.g. 'pptx slide 3 cell B2')."
    )
    extra: dict[str, Any] = Field(default_factory=dict)


class DateExtraction(BaseModel):
    """A date the agent extracted from input."""

    iso_date: str = Field(description="YYYY-MM-DD.")
    role: str | None = Field(default=None, description="schedule_start | go_live | ...")
    raw_source: str | None = None


class FileResolution(BaseModel):
    """How a dropped file was matched to an entity."""

    path: str
    matched_to: str | None = None
    matched_kind: str | None = None  # "thumbnail" | "asset" | ...


class CsvAttachment(BaseModel):
    """Structured contents of an attached CSV file."""

    path: str
    headers: list[str] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(
        default_factory=list,
        description="Each row as a {header: value} dict. Capped at ~100 rows.",
    )


class IntakeEntities(BaseModel):
    """Top-level intake stage result."""

    content_items: list[ContentItem] = Field(default_factory=list)
    dates: list[DateExtraction] = Field(default_factory=list)
    files_resolved: list[FileResolution] = Field(default_factory=list)
    csv_attachments: list[CsvAttachment] = Field(
        default_factory=list,
        description="Structured per-CSV row-level data so the planner can map "
        "row 1 to slot 1, etc., without guessing.",
    )
    raw_text_excerpts: list[str] = Field(
        default_factory=list,
        description="Up to N short snippets the LLM retained for grounding.",
    )
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Planner stage outputs
# ---------------------------------------------------------------------------


class DestructiveAction(BaseModel):
    """A destructive action declared by a skill, surfaced into the plan."""

    step_idx: int
    kind: str = Field(description="publish | delete | archive | save | ...")
    reversible: bool = False
    label: str | None = None
    affected_count: int | None = None


class PlanStep(BaseModel):
    """One invocation of a skill with concrete params."""

    idx: int
    skill_id: str
    params: dict[str, Any]
    param_sources: dict[str, str] = Field(
        default_factory=dict,
        description="Per-parameter provenance string for plan auditing.",
    )
    notes: str | None = None


class SkillInvocationSummary(BaseModel):
    skill_id: str
    invocations: int
    description: str | None = None


class Plan(BaseModel):
    """The auditable plan presented to the operator for approval."""

    id: str
    summary: str
    skill_summary: list[SkillInvocationSummary] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    destructive_actions: list[DestructiveAction] = Field(default_factory=list)
    estimated_duration_seconds: int = 0
    preconditions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Step progress (used internally + mapped to protocol events)
# ---------------------------------------------------------------------------


class StepProgress(BaseModel):
    idx: int
    action: str
    test_id: str | None = None
    screenshot_path: str | None = None
    note: str | None = None


class StepFailure(BaseModel):
    idx: int
    error_kind: str
    error_message: str
    screenshot_path: str | None = None
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
