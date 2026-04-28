"""Pydantic schemas for the agent layer.

These are the typed payloads exchanged between agent stages
(intake -> planner -> clarify -> orchestrator) and over the
JSON-RPC protocol with the host (Electron app or CLI).

Schema groups:
  - protocol.py        - on-the-wire JSON-RPC messages (DOCS/PROTOCOL.md)
  - domain.py          - in-process types (entities, plans, etc.)
  - skill.py           - intelligent skill schema v2
  - portal_context.py  - per-portal grounding file
"""

from pilot.agent.schemas.portal_context import (  # noqa: F401
    FieldConvention,
    GlossaryEntry,
    PortalContext,
    PortalPageEntry,
    SessionInfo,
    render_for_prompt as render_portal_context,
)
from pilot.agent.schemas.skill import (  # noqa: F401
    DestructiveActionSpec,
    SKILL_SCHEMA_VERSION,
    SkillFile,
    SkillParameter,
    SkillStepRef,
    SuccessAssertion,
    upgrade_v1_to_v2,
)

from pilot.agent.schemas.domain import (  # noqa: F401
    ContentItem,
    DateExtraction,
    DestructiveAction,
    FileResolution,
    IntakeEntities,
    Plan,
    PlanStep,
    SkillInvocationSummary,
    StepFailure,
    StepProgress,
)
from pilot.agent.schemas.protocol import (  # noqa: F401
    AgentEvent,
    AgentHeartbeat,
    AgentLog,
    AgentReady,
    ClarifyAnswer,
    ClarifyAsk,
    ClarifyOption,
    ClarifyQuestion,
    HostCommand,
    IntakeExtracted,
    Paused,
    PlanApprove,
    PlanProposed,
    PlanReject,
    PauseResolve,
    ReportReady,
    StepFailedEvent,
    StepProgressEvent,
    StepStartedEvent,
    StepSucceededEvent,
    TaskCancel,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
    TaskSubmit,
)

__all__ = [
    # domain
    "ContentItem",
    "DateExtraction",
    "DestructiveAction",
    "FileResolution",
    "IntakeEntities",
    "Plan",
    "PlanStep",
    "SkillInvocationSummary",
    "StepFailure",
    "StepProgress",
    # protocol
    "AgentEvent",
    "AgentHeartbeat",
    "AgentLog",
    "AgentReady",
    "ClarifyAnswer",
    "ClarifyAsk",
    "ClarifyOption",
    "ClarifyQuestion",
    "HostCommand",
    "IntakeExtracted",
    "Paused",
    "PauseResolve",
    "PlanApprove",
    "PlanProposed",
    "PlanReject",
    "ReportReady",
    "StepFailedEvent",
    "StepProgressEvent",
    "StepStartedEvent",
    "StepSucceededEvent",
    "TaskCancel",
    "TaskCancelled",
    "TaskCompleted",
    "TaskFailed",
    "TaskSubmit",
]
