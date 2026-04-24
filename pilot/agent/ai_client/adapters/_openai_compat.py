"""Shared translation helpers for OpenAI-compatible chat APIs.

Both the ``openai`` adapter and the ``groq`` adapter speak the OpenAI
Chat Completions wire format. This module holds the message / tool /
response translation so each adapter only has to deal with its
provider's auth, base URL, and quirks.

Consumers are expected to use the async ``openai`` SDK's ``AsyncOpenAI``
client (or any Groq/etc. client that mirrors the same shape).
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Iterable

from pilot.agent.ai_client.base import (
    Completion,
    Message,
    StreamChunk,
    ToolCall,
    ToolDef,
    Usage,
)


def to_openai_messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for msg in messages:
        if msg.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id or "",
                    "content": msg.content or "",
                }
            )
            continue

        entry: dict[str, Any] = {"role": msg.role}
        if msg.content is not None:
            entry["content"] = msg.content
        if msg.name:
            entry["name"] = msg.name
        if msg.role == "assistant" and msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in msg.tool_calls
            ]
            # OpenAI requires content to be null (not missing) for
            # assistant messages that only emit tool_calls.
            entry.setdefault("content", None)
        out.append(entry)
    return out


def to_openai_tools(tools: list[ToolDef] | None) -> list[dict] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters_schema,
            },
        }
        for t in tools
    ]


def parse_completion(response: Any, *, provider: str) -> Completion:
    """Translate an OpenAI SDK chat.completions response to Completion."""
    choice = response.choices[0]
    msg = choice.message
    tool_calls_out: list[ToolCall] = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {"_raw": tc.function.arguments or ""}
        tool_calls_out.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

    usage_attr = getattr(response, "usage", None)
    if usage_attr is not None:
        usage = Usage(
            prompt_tokens=getattr(usage_attr, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage_attr, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage_attr, "total_tokens", 0) or 0,
        )
    else:
        usage = Usage()

    return Completion(
        text=msg.content or "",
        tool_calls=tool_calls_out,
        finish_reason=choice.finish_reason,
        model=getattr(response, "model", None),
        usage=usage,
        provider=provider,
    )


async def iter_openai_stream(
    stream: AsyncIterator[Any], *, provider: str
) -> AsyncIterator[StreamChunk]:
    """Translate an OpenAI-compatible streaming iterator to StreamChunks.

    OpenAI streams deltas inside ``choices[0].delta``. Tool-call arguments
    arrive as a string that is concatenated across chunks; we accumulate
    per index and emit one full ToolCall when the tool_calls list for
    that index is closed (the stream doesn't mark closure explicitly, so
    we emit when we see finish_reason or when the stream ends).
    """
    partial: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: Usage | None = None

    async for event in stream:
        choices = getattr(event, "choices", None) or []
        if not choices:
            # Some providers include a usage-only final event.
            u = getattr(event, "usage", None)
            if u is not None:
                usage = Usage(
                    prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                    total_tokens=getattr(u, "total_tokens", 0) or 0,
                )
            continue

        choice = choices[0]
        delta = choice.delta

        if getattr(delta, "content", None):
            yield StreamChunk(text_delta=delta.content)

        for tc in getattr(delta, "tool_calls", None) or []:
            idx = tc.index
            buf = partial.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.id:
                buf["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    buf["name"] = fn.name
                if getattr(fn, "arguments", None):
                    buf["args"] += fn.arguments

        if choice.finish_reason:
            finish_reason = choice.finish_reason
            for idx, buf in sorted(partial.items()):
                try:
                    args = json.loads(buf["args"]) if buf["args"] else {}
                except json.JSONDecodeError:
                    args = {"_raw": buf["args"]}
                yield StreamChunk(
                    tool_call=ToolCall(
                        id=buf["id"], name=buf["name"], arguments=args
                    )
                )
            partial.clear()

    yield StreamChunk(done=True, finish_reason=finish_reason, usage=usage)
