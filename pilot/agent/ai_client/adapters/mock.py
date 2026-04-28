"""Mock AIClient for unit tests + offline development.

Returns canned responses based on a programmable rule list. Each rule is
a (predicate, response) tuple; the first matching rule wins. Defaults
to a fixed plain-text response when no rule matches.

Usage::

    from pilot.agent.ai_client.adapters.mock import MockClient

    client = MockClient()
    client.expect_text("Hello, mock world.")
    client.expect_when(
        lambda msgs: any('paris' in (m.content or '').lower() for m in msgs),
        text='{"city":"Paris","country":"France"}',
    )

    # Or for structured outputs, drop in a JSON literal that matches
    # the response_model schema:
    client.expect_text('{"city":"Paris",...}')

The mock also tracks calls for assertions::

    assert client.call_count == 1
    last = client.calls[-1]
    assert last.model == "mock"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

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


Predicate = Callable[[list[Message]], bool]


@dataclass
class _Rule:
    predicate: Predicate | None
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=lambda: Usage(1, 1, 2))
    delay_s: float = 0.0


@dataclass
class _Call:
    messages: list[Message]
    model: str | None
    temperature: float
    tools: list[ToolDef] | None


class MockClient(BaseAIClient):
    name = "mock"
    default_model = "mock"

    def __init__(self) -> None:
        self._rules: list[_Rule] = []
        self._fallback: _Rule | None = _Rule(
            predicate=None, text="(mock fallback response)"
        )
        self.calls: list[_Call] = []

    # ---- Programming the mock --------------------------------------------

    def expect_text(self, text: str, *, finish_reason: str = "stop") -> "MockClient":
        """Always respond with this text (fallback). Replaces any prior fallback."""
        self._fallback = _Rule(predicate=None, text=text, finish_reason=finish_reason)
        return self

    def expect_when(
        self,
        predicate: Predicate,
        *,
        text: str = "",
        tool_calls: list[ToolCall] | None = None,
        finish_reason: str = "stop",
    ) -> "MockClient":
        """Respond with `text` when `predicate(messages)` is true."""
        self._rules.append(
            _Rule(
                predicate=predicate,
                text=text,
                tool_calls=list(tool_calls or []),
                finish_reason=finish_reason,
            )
        )
        return self

    def reset(self) -> None:
        self.calls.clear()

    @property
    def call_count(self) -> int:
        return len(self.calls)

    # ---- Internal --------------------------------------------------------

    def _select_rule(self, messages: list[Message]) -> _Rule:
        for rule in self._rules:
            if rule.predicate is not None and rule.predicate(messages):
                return rule
        assert self._fallback is not None
        return self._fallback

    # ---- AIClient surface -----------------------------------------------

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
        self.calls.append(
            _Call(messages=list(messages), model=model, temperature=temperature, tools=tools)
        )
        rule = self._select_rule(messages)
        return Completion(
            text=rule.text,
            tool_calls=list(rule.tool_calls),
            finish_reason=rule.finish_reason,
            model=model or self.default_model,
            usage=rule.usage,
            provider="mock",
            latency_ms=0,
        )

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
        self.calls.append(
            _Call(messages=list(messages), model=model, temperature=temperature, tools=tools)
        )
        rule = self._select_rule(messages)

        async def _gen() -> AsyncIterator[StreamChunk]:
            # split text into ~32-char slices to simulate streaming
            text = rule.text
            chunk = 32
            for i in range(0, len(text), chunk):
                yield StreamChunk(text_delta=text[i : i + chunk])
            for tc in rule.tool_calls:
                yield StreamChunk(tool_call=tc)
            yield StreamChunk(done=True, finish_reason=rule.finish_reason, usage=rule.usage)

        return _gen()


def _factory() -> AIClient:
    return MockClient()


register_client("mock", _factory)
