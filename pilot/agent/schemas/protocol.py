"""On-the-wire JSON-RPC protocol payloads.

See ``DOCS/PROTOCOL.md`` for the human-readable spec. This module
defines Pydantic models matching every command and event documented
there. The agent and host serialize/deserialize through these models
so missing or extra fields surface as validation errors immediately
instead of silent drift.

Naming convention:
  - Events that flow agent->host end with `Event` *only* if their bare
    name would collide with a domain type (e.g. StepFailedEvent, since
    StepFailure already exists in domain.py).
  - Commands that flow host->agent are named with their action verb
    (e.g. TaskSubmit, PlanApprove).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Union

from pydantic import BaseModel, Field

from pilot.agent.schemas.domain import (
    DestructiveAction,
    IntakeEntities,
    PlanStep,
    SkillInvocationSummary,
)


PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# Base envelope
# ---------------------------------------------------------------------------


class _Envelope(BaseModel):
    v: int = PROTOCOL_VERSION
    type: str
    ts: str | None = None  # ISO-8601 UTC; populated on agent->host emits

    def stamp(self) -> "_Envelope":
        if self.ts is None:
            self.ts = datetime.now(timezone.utc).isoformat()
        return self


# ===========================================================================
# Host -> Agent commands
# ===========================================================================


class Attachment(BaseModel):
    path: str
    kind: str | None = None  # "pptx" | "csv" | "folder" | "image" | "pdf"


class TaskOptions(BaseModel):
    auto_approve_plan: bool = False
    screenshot_every_step: bool = True


class TaskSubmit(_Envelope):
    type: Literal["task.submit"] = "task.submit"
    task_id: str
    goal: str
    attachments: list[Attachment] = Field(default_factory=list)
    portal_id: str | None = None
    options: TaskOptions = Field(default_factory=TaskOptions)


class ClarifyAnswer(_Envelope):
    type: Literal["clarify.answer"] = "clarify.answer"
    task_id: str
    question_id: str
    answer_value: str
    answer_label: str | None = None


class PlanApprove(_Envelope):
    type: Literal["plan.approve"] = "plan.approve"
    task_id: str
    plan_id: str


class PlanReject(_Envelope):
    type: Literal["plan.reject"] = "plan.reject"
    task_id: str
    plan_id: str
    reason: str | None = None


class PauseResolve(_Envelope):
    type: Literal["pause.resolve"] = "pause.resolve"
    task_id: str
    pause_id: str
    action: Literal["retry", "skip", "abort", "use_alternate"]
    payload: dict[str, Any] | None = None


class TaskCancel(_Envelope):
    type: Literal["task.cancel"] = "task.cancel"
    task_id: str


HostCommand = Union[
    TaskSubmit,
    ClarifyAnswer,
    PlanApprove,
    PlanReject,
    PauseResolve,
    TaskCancel,
]


# ===========================================================================
# Agent -> Host events
# ===========================================================================


class AgentCapabilities(BaseModel):
    ai_clients: list[str]
    default_client: str
    supports_attachments: list[str]


class AgentReady(_Envelope):
    type: Literal["agent.ready"] = "agent.ready"
    agent_version: str
    capabilities: AgentCapabilities


class AgentHeartbeat(_Envelope):
    type: Literal["agent.heartbeat"] = "agent.heartbeat"
    status: Literal["idle", "busy"] = "idle"


class AgentLog(_Envelope):
    type: Literal["agent.log"] = "agent.log"
    level: Literal["debug", "info", "warn", "error"] = "info"
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class IntakeExtracted(_Envelope):
    type: Literal["intake.extracted"] = "intake.extracted"
    task_id: str
    entities: IntakeEntities
    intake_warnings: list[str] = Field(default_factory=list)


class ClarifyOption(BaseModel):
    value: str
    label: str
    detail: str | None = None


class ClarifyQuestion(BaseModel):
    id: str
    question: str
    options: list[ClarifyOption] = Field(default_factory=list)
    allow_custom_answer: bool = True
    priority: Literal["low", "medium", "high"] = "medium"


class ClarifyAsk(_Envelope):
    type: Literal["clarify.ask"] = "clarify.ask"
    task_id: str
    id: str
    question: str
    options: list[ClarifyOption] = Field(default_factory=list)
    allow_custom_answer: bool = True
    priority: Literal["low", "medium", "high"] = "medium"


class PlanProposed(_Envelope):
    type: Literal["plan.proposed"] = "plan.proposed"
    task_id: str
    id: str
    summary: str
    skill_summary: list[SkillInvocationSummary] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    destructive_actions: list[DestructiveAction] = Field(default_factory=list)
    estimated_duration_seconds: int = 0
    preconditions: list[str] = Field(default_factory=list)


class StepStartedEvent(_Envelope):
    type: Literal["step.started"] = "step.started"
    task_id: str
    idx: int
    skill_id: str
    params: dict[str, Any]


class StepProgressEvent(_Envelope):
    type: Literal["step.progress"] = "step.progress"
    task_id: str
    idx: int
    action: str
    test_id: str | None = None
    screenshot_path: str | None = None


class StepSucceededEvent(_Envelope):
    type: Literal["step.succeeded"] = "step.succeeded"
    task_id: str
    idx: int
    duration_ms: int = 0


class StepFailedEvent(_Envelope):
    type: Literal["step.failed"] = "step.failed"
    task_id: str
    idx: int
    error_kind: str
    error_message: str
    screenshot_path: str | None = None
    suggestions: list[dict[str, Any]] = Field(default_factory=list)


class Paused(_Envelope):
    type: Literal["paused"] = "paused"
    task_id: str
    pause_id: str
    reason: str
    context: dict[str, Any] = Field(default_factory=dict)


class ReportReady(_Envelope):
    type: Literal["report.ready"] = "report.ready"
    task_id: str
    session_id: str
    report_path: str
    summary: str
    warnings: list[str] = Field(default_factory=list)


class TaskCompleted(_Envelope):
    type: Literal["task.completed"] = "task.completed"
    task_id: str
    session_id: str | None = None


class TaskFailed(_Envelope):
    type: Literal["task.failed"] = "task.failed"
    task_id: str
    error_kind: str
    error_message: str


class TaskCancelled(_Envelope):
    type: Literal["task.cancelled"] = "task.cancelled"
    task_id: str


AgentEvent = Union[
    AgentReady,
    AgentHeartbeat,
    AgentLog,
    IntakeExtracted,
    ClarifyAsk,
    PlanProposed,
    StepStartedEvent,
    StepProgressEvent,
    StepSucceededEvent,
    StepFailedEvent,
    Paused,
    ReportReady,
    TaskCompleted,
    TaskFailed,
    TaskCancelled,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_HOST_TYPE_MAP: dict[str, type[BaseModel]] = {
    "task.submit": TaskSubmit,
    "clarify.answer": ClarifyAnswer,
    "plan.approve": PlanApprove,
    "plan.reject": PlanReject,
    "pause.resolve": PauseResolve,
    "task.cancel": TaskCancel,
}


def parse_host_command(payload: dict[str, Any]) -> HostCommand:
    """Parse a JSON-RPC host->agent command into the right Pydantic
    model. Raises ValueError on unknown type, ValidationError on malformed
    payload."""
    t = payload.get("type")
    if t not in _HOST_TYPE_MAP:
        raise ValueError(f"unknown host command type: {t!r}")
    return _HOST_TYPE_MAP[t].model_validate(payload)
