"""AIClient interface + adapters.

The agent talks to LLMs exclusively through the AIClient Protocol.
Concrete adapters (Bedrock, Groq, OpenAI, your org's custom LLM) live
in pilot.agent.ai_client.adapters and translate the Protocol's
neutral shape into each provider's native API.

Usage::

    from pilot.agent.ai_client import get_client, Message

    client = get_client("bedrock")          # named adapter
    # or
    client = get_client()                    # default from config

    resp = await client.complete(
        messages=[Message(role="user", content="Hello")],
        model="anthropic.claude-sonnet-4-20250929-v1:0",
    )
    print(resp.text)

Structured outputs::

    from pydantic import BaseModel
    from pilot.agent.ai_client import complete_structured

    class Plan(BaseModel):
        steps: list[str]

    plan = await complete_structured(
        client,
        messages=[Message(role="user", content="Plan a trip to Paris")],
        model="anthropic.claude-sonnet-4-20250929-v1:0",
        response_model=Plan,
    )
"""

from pilot.agent.ai_client.base import (  # noqa: F401
    AIClient,
    Completion,
    Message,
    Role,
    StreamChunk,
    ToolCall,
    ToolDef,
)
from pilot.agent.ai_client.registry import get_client, register_client  # noqa: F401
from pilot.agent.ai_client.structured import complete_structured  # noqa: F401

__all__ = [
    "AIClient",
    "Completion",
    "Message",
    "Role",
    "StreamChunk",
    "ToolCall",
    "ToolDef",
    "complete_structured",
    "get_client",
    "register_client",
]
