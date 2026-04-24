"""Provider-agnostic structured-output helper.

Rather than depending on `instructor` (which wraps specific clients in
ways that don't always mesh with custom-format LLMs), we force JSON
Schema compliance via prompt engineering + Pydantic validation + a
limited retry loop.

This works across any AIClient adapter, including ones for LLMs with
non-standard interaction formats, because it only uses `complete()`.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from pilot.agent.ai_client.base import AIClient, Message

T = TypeVar("T", bound=BaseModel)


# Matches the most common ways models wrap JSON:
#   ```json\n{...}\n```
#   ```\n{...}\n```
#   plain {...} (possibly preceded by explanation prose)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


class StructuredOutputError(Exception):
    """Raised when the model cannot produce valid JSON matching the
    requested schema after all retries."""

    def __init__(self, message: str, *, last_response: str | None = None):
        super().__init__(message)
        self.last_response = last_response


def _extract_json_blob(text: str) -> str:
    """Best-effort JSON extractor for LLM outputs.

    Tries, in order:
      1. Fenced code block (```json ... ``` or ``` ... ```)
      2. First balanced top-level {...} or [...] substring
      3. The whole string, trimmed

    Raises json.JSONDecodeError via the caller's json.loads if none of
    these produce parseable JSON.
    """
    # 1. Fenced
    m = _CODE_FENCE_RE.search(text)
    if m:
        return m.group(1)

    # 2. First balanced object or array
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

    # 3. Last resort: the whole thing
    return text.strip()


def _build_system_addon(response_model: type[BaseModel]) -> str:
    schema = response_model.model_json_schema()
    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)
    return (
        "You must respond with a single JSON value that validates "
        "against the following JSON Schema. Do not include any prose, "
        "explanation, or markdown code fences. Output JSON only.\n\n"
        f"JSON Schema:\n{schema_json}"
    )


async def complete_structured(
    client: AIClient,
    messages: list[Message],
    *,
    response_model: type[T],
    model: str | None = None,
    temperature: float = 0.0,
    max_retries: int = 2,
    timeout_s: float | None = None,
) -> T:
    """Complete a chat and parse the response into `response_model`.

    Strategy:
      - Inject a system message telling the model to produce JSON
        matching ``response_model``'s JSON Schema.
      - Call ``client.complete``.
      - Extract the JSON blob from the response (tolerates code fences
        and leading/trailing prose).
      - Validate via Pydantic.
      - On failure, append the bad response + a correction instruction
        to the message history and retry up to ``max_retries`` times.

    Raises:
        StructuredOutputError: if all retries fail.
    """
    system_addon = _build_system_addon(response_model)
    conversation: list[Message] = [
        Message(role="system", content=system_addon),
        *messages,
    ]

    last_error: Exception | None = None
    last_text: str | None = None

    for attempt in range(max_retries + 1):
        completion = await client.complete(
            conversation,
            model=model,
            temperature=temperature,
            timeout_s=timeout_s,
        )
        last_text = completion.text

        try:
            blob = _extract_json_blob(completion.text)
            data = json.loads(blob)
            return response_model.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            if attempt >= max_retries:
                break
            # Tell the model exactly what went wrong and ask for a fix.
            conversation = [
                *conversation,
                Message(role="assistant", content=completion.text),
                Message(
                    role="user",
                    content=(
                        "Your last response could not be parsed as the "
                        "required JSON. Error:\n"
                        f"{type(e).__name__}: {e}\n\n"
                        "Respond again with ONLY a valid JSON value "
                        "matching the schema. No prose, no markdown."
                    ),
                ),
            ]

    raise StructuredOutputError(
        f"structured output failed after {max_retries + 1} attempts: "
        f"{type(last_error).__name__}: {last_error}",
        last_response=last_text,
    )
