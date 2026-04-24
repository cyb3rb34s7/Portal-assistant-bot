"""AWS Bedrock adapter.

Uses the Bedrock Runtime ``Converse`` / ``ConverseStream`` APIs which
provide a unified chat-style interface across all Bedrock-hosted model
families (Anthropic Claude, Meta Llama, Amazon Nova, Mistral, etc.).

Environment configuration:
    CURATIONPILOT_BEDROCK_REGION         default: us-east-1
    CURATIONPILOT_BEDROCK_DEFAULT_MODEL  default: a recent Claude Sonnet
    AWS_PROFILE / AWS_ACCESS_KEY_ID      standard AWS credential chain

Optional deps:
    boto3 (``pip install boto3``)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator

try:
    import boto3  # type: ignore
    from botocore.config import Config as BotoConfig  # type: ignore

    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover - covered at runtime in get_client
    _BOTO3_AVAILABLE = False

from pilot.agent.ai_client.base import (
    AIClient,
    BaseAIClient,
    Completion,
    Message,
    StreamChunk,
    ToolCall,
    ToolDef,
    Usage,
)
from pilot.agent.ai_client.registry import register_client


DEFAULT_MODEL = "anthropic.claude-sonnet-4-20250929-v1:0"
DEFAULT_REGION = "us-east-1"


def _to_bedrock_messages(messages: list[Message]) -> tuple[list[dict], str | None]:
    """Split neutral messages into (non-system messages, system prompt).

    Bedrock Converse takes ``system`` as a top-level parameter. Multiple
    system messages are concatenated with blank lines.
    """
    system_parts: list[str] = []
    converted: list[dict] = []

    for msg in messages:
        if msg.role == "system":
            if msg.content:
                system_parts.append(msg.content)
            continue

        if msg.role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": msg.tool_call_id or "",
                                "content": [{"text": msg.content or ""}],
                                "status": "success",
                            }
                        }
                    ],
                }
            )
            continue

        # assistant or user
        content_blocks: list[dict] = []
        if msg.content:
            content_blocks.append({"text": msg.content})

        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                content_blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    }
                )

        if not content_blocks:
            # Bedrock rejects empty content arrays.
            content_blocks = [{"text": ""}]

        converted.append({"role": msg.role, "content": content_blocks})

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return converted, system_prompt


def _to_bedrock_tools(tools: list[ToolDef] | None) -> dict | None:
    if not tools:
        return None
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": {"json": t.parameters_schema},
                }
            }
            for t in tools
        ]
    }


def _parse_completion(response: dict) -> Completion:
    """Translate a Bedrock Converse response to a Completion."""
    output = response.get("output", {}).get("message", {})
    content_blocks = output.get("content", []) or []

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append(
                ToolCall(
                    id=tu.get("toolUseId", ""),
                    name=tu.get("name", ""),
                    arguments=tu.get("input") or {},
                )
            )

    usage_data = response.get("usage", {}) or {}
    usage = Usage(
        prompt_tokens=usage_data.get("inputTokens", 0),
        completion_tokens=usage_data.get("outputTokens", 0),
        total_tokens=usage_data.get("totalTokens")
        or (usage_data.get("inputTokens", 0) + usage_data.get("outputTokens", 0)),
    )

    return Completion(
        text="".join(text_parts),
        tool_calls=tool_calls,
        finish_reason=response.get("stopReason"),
        model=response.get("modelId"),
        usage=usage,
        provider="bedrock",
    )


class BedrockClient(BaseAIClient):
    name = "bedrock"

    def __init__(
        self,
        *,
        region: str | None = None,
        default_model: str | None = None,
        read_timeout_s: float = 60.0,
    ):
        if not _BOTO3_AVAILABLE:
            raise ImportError(
                "BedrockClient requires boto3. Install it with "
                "`pip install boto3`."
            )

        self.region = region or os.environ.get(
            "CURATIONPILOT_BEDROCK_REGION", DEFAULT_REGION
        )
        self.default_model = (
            default_model
            or os.environ.get("CURATIONPILOT_BEDROCK_DEFAULT_MODEL")
            or DEFAULT_MODEL
        )
        boto_config = BotoConfig(
            read_timeout=read_timeout_s,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self._runtime = boto3.client(
            "bedrock-runtime", region_name=self.region, config=boto_config
        )

    def _resolve_model(self, model: str | None) -> str:
        if model:
            return model
        if self.default_model:
            return self.default_model
        raise ValueError(
            "BedrockClient.complete() needs an explicit model (or set "
            "CURATIONPILOT_BEDROCK_DEFAULT_MODEL)."
        )

    def _inference_config(
        self, temperature: float, max_tokens: int | None, stop: list[str] | None
    ) -> dict[str, Any]:
        cfg: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            cfg["maxTokens"] = max_tokens
        if stop:
            cfg["stopSequences"] = stop
        return cfg

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
        model_id = self._resolve_model(model)
        bedrock_messages, system_prompt = _to_bedrock_messages(messages)

        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": bedrock_messages,
            "inferenceConfig": self._inference_config(temperature, max_tokens, stop),
        }
        if system_prompt is not None:
            kwargs["system"] = [{"text": system_prompt}]
        tool_cfg = _to_bedrock_tools(tools)
        if tool_cfg:
            kwargs["toolConfig"] = tool_cfg

        start = time.time()
        # boto3 Converse is synchronous; run it in a worker thread.
        resp = await asyncio.to_thread(self._runtime.converse, **kwargs)
        latency_ms = int((time.time() - start) * 1000)

        completion = _parse_completion(resp)
        completion.latency_ms = latency_ms
        if not completion.model:
            completion.model = model_id
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
        model_id = self._resolve_model(model)
        bedrock_messages, system_prompt = _to_bedrock_messages(messages)

        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": bedrock_messages,
            "inferenceConfig": self._inference_config(temperature, max_tokens, stop),
        }
        if system_prompt is not None:
            kwargs["system"] = [{"text": system_prompt}]
        tool_cfg = _to_bedrock_tools(tools)
        if tool_cfg:
            kwargs["toolConfig"] = tool_cfg

        # ConverseStream's event stream is synchronous; we pump it to a
        # queue and consume the queue from async code.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[StreamChunk | Exception | None] = asyncio.Queue()

        def _pump() -> None:
            try:
                response = self._runtime.converse_stream(**kwargs)
                tool_calls_partial: dict[int, dict[str, Any]] = {}
                finish_reason: str | None = None
                usage: Usage | None = None

                for event in response.get("stream", []):
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"]["delta"]
                        if "text" in delta:
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                StreamChunk(text_delta=delta["text"]),
                            )
                        elif "toolUse" in delta:
                            idx = event["contentBlockDelta"]["contentBlockIndex"]
                            buf = tool_calls_partial.setdefault(
                                idx, {"id": "", "name": "", "args": ""}
                            )
                            buf["args"] += delta["toolUse"].get("input", "")
                    elif "contentBlockStart" in event:
                        start_info = event["contentBlockStart"]["start"]
                        if "toolUse" in start_info:
                            idx = event["contentBlockStart"]["contentBlockIndex"]
                            tu = start_info["toolUse"]
                            buf = tool_calls_partial.setdefault(
                                idx, {"id": "", "name": "", "args": ""}
                            )
                            buf["id"] = tu.get("toolUseId", "")
                            buf["name"] = tu.get("name", "")
                    elif "contentBlockStop" in event:
                        idx = event["contentBlockStop"]["contentBlockIndex"]
                        if idx in tool_calls_partial:
                            buf = tool_calls_partial.pop(idx)
                            try:
                                args = json.loads(buf["args"]) if buf["args"] else {}
                            except json.JSONDecodeError:
                                args = {"_raw": buf["args"]}
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                StreamChunk(
                                    tool_call=ToolCall(
                                        id=buf["id"],
                                        name=buf["name"],
                                        arguments=args,
                                    )
                                ),
                            )
                    elif "messageStop" in event:
                        finish_reason = event["messageStop"].get("stopReason")
                    elif "metadata" in event:
                        u = event["metadata"].get("usage", {}) or {}
                        usage = Usage(
                            prompt_tokens=u.get("inputTokens", 0),
                            completion_tokens=u.get("outputTokens", 0),
                            total_tokens=u.get("totalTokens")
                            or (
                                u.get("inputTokens", 0)
                                + u.get("outputTokens", 0)
                            ),
                        )

                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    StreamChunk(done=True, finish_reason=finish_reason, usage=usage),
                )
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as e:  # pragma: no cover - forwarded
                loop.call_soon_threadsafe(queue.put_nowait, e)

        task = asyncio.create_task(asyncio.to_thread(_pump))

        async def _iter() -> AsyncIterator[StreamChunk]:
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    if isinstance(item, Exception):
                        raise item
                    yield item
                    if item.done:
                        return
            finally:
                await task

        return _iter()


def _factory() -> AIClient:
    return BedrockClient()


register_client("bedrock", _factory)
