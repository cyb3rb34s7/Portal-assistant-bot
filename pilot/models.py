"""Typed models shared across the pilot package.

These models define the contract between the runner, the adapters, and the
audit logger. They deliberately mirror the shapes described in the
architecture doc so the POC code can be promoted without rewrite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Standard return type for every adapter method.

    Rule: every adapter method returns exactly this shape. The runner and
    audit logger depend on it. Do not return anything else.
    """

    success: bool
    action_taken: str
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    screenshot_path: Optional[str] = None
    confidence: Optional[Literal["high", "medium", "low"]] = None
    # Set by skill_runner when a step's locator was self-healed (L3).
    # The dict carries: original_summary, new_summary, confidence,
    # reason, post_condition_passed, new_fingerprint (full).
    # Consumed by orchestrators to emit StepHealed + persist alternates.
    healed: Optional[dict[str, Any]] = None


class Task(BaseModel):
    """A single task to execute in the runner.

    The `action` field names the adapter method to call. The `params` dict
    is spread into the method's keyword arguments.
    """

    task_id: str
    adapter: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    requires_approval_gate: bool = False
    expected_result: str = ""
    status: Literal[
        "pending", "running", "done", "failed", "skipped", "rejected"
    ] = "pending"


class TaskList(BaseModel):
    """A named, ordered list of tasks loaded from a JSON file."""

    name: str
    description: str = ""
    tasks: list[Task]


class AuditEvent(BaseModel):
    """A single entry written to the session's audit log."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    session_id: str
    task_id: Optional[str] = None
    kind: str  # "task_start" / "task_end" / "gate" / "error" / "info"
    message: str
    data: Optional[dict[str, Any]] = None
