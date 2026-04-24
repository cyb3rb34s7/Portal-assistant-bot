"""CurationPilot agent layer.

This package adds LLM-driven reasoning on top of the deterministic
pilot runner. The core commitment is: LLMs reason at the edges
(intake, planner, clarify, reporter, repair); the runner executes
recorded skills deterministically in the middle.

Import surface:
    from pilot.agent.ai_client import (
        AIClient,          # Protocol
        Message,           # dataclass
        ToolDef,           # dataclass
        Completion,        # dataclass
        StreamChunk,       # dataclass
        get_client,        # factory
        complete_structured,  # provider-agnostic structured-output helper
    )
"""

from pilot.agent.ai_client import (  # noqa: F401
    AIClient,
    Completion,
    Message,
    StreamChunk,
    ToolCall,
    ToolDef,
    complete_structured,
    get_client,
)

__all__ = [
    "AIClient",
    "Completion",
    "Message",
    "StreamChunk",
    "ToolCall",
    "ToolDef",
    "complete_structured",
    "get_client",
]
