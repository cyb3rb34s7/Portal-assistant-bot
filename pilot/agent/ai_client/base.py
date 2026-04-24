"""Protocol + data classes for the AIClient abstraction.

Design goals:

1. Provider-agnostic. Each adapter (Bedrock, Groq, OpenAI, org custom)
   translates these neutral types to its own wire format.
2. Minimal surface. Only the methods the agent orchestrator actually
   uses: `complete`, `stream`, and `complete_structured` (the last one
   is supplied by ``structured.py`` as a provider-agnostic helper so
   adapters don't each have to reinvent it).
3. No framework lock-in. Everything here is stdlib + Pydantic.
4. Auditable. Every response carries usage + latency so sessions can
   log tokens and cost per call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


# ---------------------------------------------------------------------------
# Messages + tools
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A single message in a chat-style exchange.

    Attributes:
        role:  "system" | "user" | "assistant" | "tool".
        content: plain text body of the message. None when this message
            is a pure tool_call emission from the assistant (tool_calls
            carries the calls instead).
        name: optional author name; for "tool" role this is the tool name
            the tool_result refers to.
        tool_call_id: for role="tool" messages, the id of the tool call
            this message is the result of.
        tool_calls: for role="assistant" messages, the list of tool calls
            the model wants to invoke.
    """

    role: Role
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolDef:
    """Declaration of a tool the model may call.

    Kept intentionally close to OpenAI's function-calling shape because
    most providers either speak it natively or have a trivial mapping.
    Adapters are responsible for any conversion.
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]
    """JSON Schema for the tool's parameters object."""


@dataclass
class ToolCall:
    """A tool call requested by the assistant."""

    id: str
    name: str
    arguments: dict[str, Any]
    """Parsed JSON arguments. Adapters are responsible for JSON parsing
    and raising a clean error if the model produced malformed JSON."""


# ---------------------------------------------------------------------------
# Completions
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Token accounting for a single LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    """Providers that don't report total_tokens should set it to
    prompt_tokens + completion_tokens."""


@dataclass
class Completion:
    """A full (non-streaming) LLM response."""

    text: str
    """The assistant's text output. Empty string when the model chose to
    emit only tool_calls."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    """Tool calls the assistant wants executed, in the order emitted."""

    finish_reason: str | None = None
    """Provider-specific, surfaced for diagnostics. Examples: "stop",
    "length", "tool_calls", "content_filter". Not used for control flow
    by the orchestrator."""

    model: str | None = None
    """Model id actually used (as reported by provider; may differ from
    requested alias)."""

    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0

    provider: str | None = None
    """Name of the adapter that produced this completion (e.g. "bedrock",
    "groq"). Useful for audit logs."""

    raw: dict[str, Any] | None = None
    """Optional raw provider response for debugging. Adapters may leave
    this None in production to avoid logging sensitive data."""


@dataclass
class StreamChunk:
    """Incremental output during streaming.

    Exactly one of the following fields is populated per chunk:
    - text_delta: next slice of assistant text
    - tool_call_delta: next slice of a tool-call (adapters should
      accumulate partial arguments and emit a whole ToolCall in the
      `tool_call` field once complete, to keep downstream parsing simple)
    - done: sentinel chunk with final usage + finish_reason
    """

    text_delta: str | None = None
    tool_call: ToolCall | None = None
    done: bool = False
    finish_reason: str | None = None
    usage: Usage | None = None


# ---------------------------------------------------------------------------
# The Protocol itself
# ---------------------------------------------------------------------------


@runtime_checkable
class AIClient(Protocol):
    """Neutral interface every provider adapter implements.

    Implementations must be safe to use concurrently (i.e. share one
    client across many tasks); adapters typically wrap an httpx or boto3
    client that is already thread/async-safe.
    """

    name: str
    """Short identifier for the adapter ("bedrock", "groq", "openai",
    etc.). Used in audit logs and for per-stage routing."""

    default_model: str | None
    """Optional default model id for this client. If set, callers may
    omit the `model` kwarg; otherwise they must pass one explicitly."""

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: list[ToolDef] | None = None,
        stop: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> Completion:
        """Non-streaming chat completion."""
        ...

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        tools: list[ToolDef] | None = None,
        stop: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming chat completion.

        Yields StreamChunk values until a chunk with ``done=True`` is
        emitted (which also carries final usage + finish_reason).
        """
        ...

    async def close(self) -> None:
        """Release any underlying transport resources."""
        ...


# ---------------------------------------------------------------------------
# Convenience base class
# ---------------------------------------------------------------------------


class BaseAIClient:
    """Optional base class with sensible defaults for adapter authors.

    Adapters don't have to inherit from this — implementing the Protocol
    is sufficient — but this covers the common boilerplate (name,
    default_model, close() no-op, __aenter__/__aexit__).
    """

    name: str = "unnamed"
    default_model: str | None = None

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> "BaseAIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
