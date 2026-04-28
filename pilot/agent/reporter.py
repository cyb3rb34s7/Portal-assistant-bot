"""Reporter stage.

After execution, produces a markdown summary of the run from the
session's audit log + step events + screenshots. Optionally enriched
by an LLM for natural-language framing; falls back to a deterministic
template if no client / model available.

The deterministic fallback is good enough to ship; the LLM enhancement
is mostly for narrative tone in the final user-facing report.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pilot.agent.ai_client import AIClient, Message
from pilot.agent.ai_client.structured import StructuredOutputError, complete_structured
from pydantic import BaseModel, Field


class _LlmReportShape(BaseModel):
    """Schema the LLM is asked to populate. Kept small to keep latency
    + cost low."""

    headline: str = Field(description="One-sentence summary of the run.")
    paragraphs: list[str] = Field(
        description="2-4 short paragraphs of plain-English commentary.",
        min_length=1,
        max_length=4,
    )


def _format_step_summary(steps: list[dict]) -> list[str]:
    """Turn a list of step events into bulletable summary lines."""
    lines: list[str] = []
    for s in steps:
        idx = s.get("idx", "?")
        skill_id = s.get("skill_id", "?")
        status = s.get("status", "?")
        duration_ms = s.get("duration_ms")
        suffix = f" ({duration_ms}ms)" if duration_ms is not None else ""
        params = s.get("params") or {}
        param_str = ", ".join(f"{k}={v!r}" for k, v in list(params.items())[:3])
        lines.append(f"- step {idx} [{status}] {skill_id} {param_str}{suffix}")
    return lines


def _deterministic_report(
    *,
    session_id: str,
    summary: str,
    steps: list[dict],
    warnings: list[str],
) -> str:
    completed = sum(1 for s in steps if s.get("status") == "succeeded")
    failed = sum(1 for s in steps if s.get("status") == "failed")
    skipped = sum(1 for s in steps if s.get("status") == "skipped")
    total = len(steps)

    parts = [
        f"# Run report — session {session_id}",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat()}_",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Outcome",
        "",
        f"- total steps: {total}",
        f"- succeeded: {completed}",
        f"- failed: {failed}",
        f"- skipped: {skipped}",
        "",
        "## Step trace",
        "",
        *_format_step_summary(steps),
        "",
    ]
    if warnings:
        parts += ["## Warnings", "", *(f"- {w}" for w in warnings), ""]
    return "\n".join(parts)


async def write_report(
    *,
    session_dir: Path,
    session_id: str,
    summary: str,
    steps: list[dict],
    warnings: list[str],
    client: AIClient | None = None,
    model: str | None = None,
) -> Path:
    """Write `report.md` into ``session_dir`` and return its path."""
    session_dir.mkdir(parents=True, exist_ok=True)
    report_path = session_dir / "report.md"

    base_md = _deterministic_report(
        session_id=session_id, summary=summary, steps=steps, warnings=warnings
    )

    final_md = base_md
    if client is not None:
        try:
            shape = await complete_structured(
                client,
                messages=[
                    Message(
                        role="system",
                        content=(
                            "You are the reporter stage. Given a deterministic "
                            "run report, generate a 1-sentence headline and 2-4 "
                            "short plain-English paragraphs. Do not invent "
                            "facts. Reflect failures and warnings honestly."
                        ),
                    ),
                    Message(
                        role="user",
                        content=f"Deterministic report below:\n\n{base_md}",
                    ),
                ],
                response_model=_LlmReportShape,
                model=model,
                temperature=0.0,
                max_retries=1,
            )
            final_md = (
                f"# {shape.headline}\n\n"
                + "\n\n".join(shape.paragraphs)
                + "\n\n---\n\n"
                + base_md
            )
        except StructuredOutputError:
            # silently fall back to the deterministic template
            pass

    report_path.write_text(final_md, encoding="utf-8")
    return report_path
