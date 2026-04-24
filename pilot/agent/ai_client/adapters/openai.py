"""OpenAI adapter.

Uses the official ``openai`` async SDK against api.openai.com (or any
OpenAI-compatible endpoint configured via ``OPENAI_BASE_URL``).

Environment:
    OPENAI_API_KEY                       required
    OPENAI_BASE_URL                      optional (defaults to OpenAI)
    CURATIONPILOT_OPENAI_DEFAULT_MODEL   default: gpt-4o-mini

Optional deps:
    openai (``pip install openai``)
"""

from __future__ import annotations

import os
import time
from typing import AsyncIterator

try:
    from openai import AsyncOpenAI  # type: ignore

    _OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OPENAI_AVAILABLE = False

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


DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIClient(BaseAIClient):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        timeout_s: float = 60.0,
    ):
        if not _OPENAI_AVAILABLE:
            raise ImportError(
                "OpenAIClient requires the openai package. Install it with "
                "`pip install openai`."
            )

        self.default_model = (
            default_model
            or os.environ.get("CURATIONPILOT_OPENAI_DEFAULT_MODEL")
            or DEFAULT_MODEL
        )
        self._client = AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            base_url=base_url or os.environ.get("OPENAI_BASE_URL"),
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
        oai_tools = to_openai_tools(tools)
        if oai_tools:
            kwargs["tools"] = oai_tools
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s

        start = time.time()
        resp = await self._client.chat.completions.create(**kwargs)
        latency_ms = int((time.time() - start) * 1000)

        completion = parse_completion(resp, provider="openai")
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
            "stream_options": {"include_usage": True},
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if stop:
            kwargs["stop"] = stop
        oai_tools = to_openai_tools(tools)
        if oai_tools:
            kwargs["tools"] = oai_tools
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s

        stream = await self._client.chat.completions.create(**kwargs)
        return iter_openai_stream(stream, provider="openai")

    async def close(self) -> None:
        await self._client.close()


def _factory() -> AIClient:
    return OpenAIClient()


register_client("openai", _factory)
