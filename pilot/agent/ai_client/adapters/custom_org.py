"""Template for a custom organisation LLM adapter.

This file is a skeleton you can fill in with the client class and
adapter you already have from another project. The only contract this
file is asked to honour is:

  1. Define a class that implements the AIClient Protocol from
     ``pilot.agent.ai_client.base``.
  2. At import time, call ``register_client("custom_org", _factory)``
     with a zero-arg factory.

Once both are in place, the registry will expose the adapter under the
name ``"custom_org"`` (rename to whatever fits) and callers can use::

    client = get_client("custom_org")

You almost certainly want to reuse the shared helpers in
``_openai_compat.py`` if your org LLM speaks the OpenAI Chat Completions
format. If it doesn't, this adapter is the one place you have to do the
translation — no other code in the agent cares what the wire format is.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

from pilot.agent.ai_client.base import (
    AIClient,
    BaseAIClient,
    Completion,
    Message,
    StreamChunk,
    ToolDef,
)
from pilot.agent.ai_client.registry import register_client


# NOTE: this skeleton does NOT call register_client. Uncomment the call
# at the bottom of the file after you replace the stub below with your
# actual client class.


class CustomOrgClient(BaseAIClient):
    """Stub implementation. Replace with your real client.

    Suggested structure when you drop in your existing code:

        class CustomOrgClient(BaseAIClient):
            name = "custom_org"
            default_model = "mycompany-llm-v2"

            def __init__(self, *, endpoint, auth_token, ...):
                self._inner = MyExistingClient(endpoint, auth_token)

            async def complete(self, messages, *, model=None, ...):
                # translate Message[] -> your wire format
                payload = translate_to_org_format(messages, model, ...)
                raw = await self._inner.invoke(payload)
                # translate raw back to Completion
                return translate_from_org_format(raw)

            async def stream(...):
                ...

            async def close(self):
                await self._inner.close()

    For OpenAI-compatible custom endpoints, just reuse the helpers in
    ``pilot.agent.ai_client.adapters._openai_compat`` and point
    ``AsyncOpenAI(base_url=...)`` at your internal endpoint.
    """

    name = "custom_org"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        auth_token: str | None = None,
        default_model: str | None = None,
    ):
        self.endpoint = endpoint or os.environ.get("CURATIONPILOT_CUSTOM_ORG_ENDPOINT")
        self.auth_token = auth_token or os.environ.get(
            "CURATIONPILOT_CUSTOM_ORG_TOKEN"
        )
        self.default_model = default_model or os.environ.get(
            "CURATIONPILOT_CUSTOM_ORG_DEFAULT_MODEL"
        )

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
        raise NotImplementedError(
            "CustomOrgClient.complete is a stub. Replace with your "
            "organisation-specific implementation."
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
        raise NotImplementedError(
            "CustomOrgClient.stream is a stub. Replace with your "
            "organisation-specific implementation."
        )


def _factory() -> AIClient:
    return CustomOrgClient()


# Uncomment after implementing the real client:
# register_client("custom_org", _factory)
