"""Groq adapter.

Uses the official async ``groq`` SDK, which mirrors the OpenAI Chat
Completions interface (including streaming + function-calling). We
reuse the shared _openai_compat helpers for translation.

Environment:
    GROQ_API_KEY                        required
    CURATIONPILOT_GROQ_DEFAULT_MODEL    default: llama-3.3-70b-versatile

Optional deps:
    groq (``pip install groq``)
"""

from __future__ import annotations

import os
import time
from typing import AsyncIterator

try:
    from groq import AsyncGroq  # type: ignore

    _GROQ_AVAILABLE = True
except ImportError:  # pragma: no cover
    _GROQ_AVAILABLE = False

from pilot.agent.ai_client.adapters._openai_compat import (
    iter_openai_stream,
    parse_completion,
    to_openai_messages,
    to_openai_tools,
)
from pilot.agent.ai_client.base import (
    AIClient,
    BaseAIClient,
    Completion,
    Message,
    StreamChunk,
    ToolDef,
)
from pilot.agent.ai_client.registry import register_client


DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqClient(BaseAIClient):
    name = "groq"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout_s: float = 60.0,
    ):
        if not _GROQ_AVAILABLE:
            raise ImportError(
                "GroqClient requires the groq package. Install it with "
                "`pip install groq`."
            )

        self.default_model = (
            default_model
            or os.environ.get("CURATIONPILOT_GROQ_DEFAULT_MODEL")
            or DEFAULT_MODEL
        )
        self._client = AsyncGroq(
            api_key=api_key or os.environ.get("GROQ_API_KEY"),
            timeout=timeout_s,
        )

    def _resolve_model(self, model: str | None) -> str:
        return model or self.default_model or DEFAULT_MODEL

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
        kwargs = {
            "model": self._resolve_model(model),
            "messages": to_openai_messages(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if stop:
            kwargs["stop"] = stop
        tools_payload = to_openai_tools(tools)
        if tools_payload:
            kwargs["tools"] = tools_payload

        start = time.time()
        resp = await self._client.chat.completions.create(**kwargs)
        latency_ms = int((time.time() - start) * 1000)

        completion = parse_completion(resp, provider="groq")
        completion.latency_ms = latency_ms
        return completion

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
        kwargs = {
            "model": self._resolve_model(model),
            "messages": to_openai_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if stop:
            kwargs["stop"] = stop
        tools_payload = to_openai_tools(tools)
        if tools_payload:
            kwargs["tools"] = tools_payload

        stream = await self._client.chat.completions.create(**kwargs)
        return iter_openai_stream(stream, provider="groq")

    async def close(self) -> None:
        # Groq's SDK mirrors httpx-based clients; close when supported.
        close_fn = getattr(self._client, "close", None)
        if close_fn is not None:
            result = close_fn()
            if hasattr(result, "__await__"):
                await result


def _factory() -> AIClient:
    return GroqClient()


register_client("groq", _factory)
