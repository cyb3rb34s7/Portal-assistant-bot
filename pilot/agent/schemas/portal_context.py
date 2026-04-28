"""Portal context schema.

Hand-authored YAML file per portal that gives the planner grounding
about the portal's domain, page map, field conventions, and risky
actions. One file per portal at ``portals/<portal_id>/context.yaml``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


PORTAL_CONTEXT_SCHEMA_VERSION = 1


class GlossaryEntry(BaseModel):
    term: str
    meaning: str
    aliases: list[str] = Field(default_factory=list)


class PortalPageEntry(BaseModel):
    path: str = Field(description="URL path, e.g. '/curation'.")
    role: str = Field(description="home | primary_workflow | asset_library | ...")
    sub_tabs: list[str] = Field(default_factory=list)
    notes: str | None = None


class FieldConvention(BaseModel):
    field: str
    format: str | None = None
    examples: list[str] = Field(default_factory=list)
    timezone: str | None = None
    enum_values: list[str] = Field(default_factory=list)


class SessionInfo(BaseModel):
    login_url: str | None = None
    session_duration_minutes: int | None = None
    sso_provider: Literal["okta", "azure_ad", "google", "saml", "none", "unknown"] = (
        "unknown"
    )
    notes: str | None = None


class PortalContext(BaseModel):
    schema_version: int = PORTAL_CONTEXT_SCHEMA_VERSION

    # Identity
    portal_id: str
    name: str
    base_url: str

    # Knowledge for planner grounding
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    page_map: list[PortalPageEntry] = Field(default_factory=list)
    field_conventions: list[FieldConvention] = Field(default_factory=list)
    destructive_actions: list[str] = Field(
        default_factory=list,
        description="Action verbs that should always be flagged as destructive.",
    )
    session: SessionInfo = Field(default_factory=SessionInfo)

    extra: dict[str, Any] = Field(default_factory=dict)


def render_for_prompt(ctx: PortalContext, *, max_chars: int = 4000) -> str:
    """Compact textual rendering of a PortalContext for inclusion in
    LLM prompts. Truncates to ``max_chars``.
    """
    lines: list[str] = []
    lines.append(f"Portal: {ctx.name} ({ctx.portal_id})")
    lines.append(f"Base URL: {ctx.base_url}")

    if ctx.glossary:
        lines.append("\nGlossary:")
        for g in ctx.glossary:
            aliases = (
                f" (also: {', '.join(g.aliases)})" if g.aliases else ""
            )
            lines.append(f"- {g.term}: {g.meaning}{aliases}")

    if ctx.page_map:
        lines.append("\nPage map:")
        for p in ctx.page_map:
            sub = (
                f" -> {', '.join(p.sub_tabs)}" if p.sub_tabs else ""
            )
            lines.append(f"- {p.path} [{p.role}]{sub}")

    if ctx.field_conventions:
        lines.append("\nField conventions:")
        for f in ctx.field_conventions:
            ex = (
                f" e.g. {', '.join(f.examples)}" if f.examples else ""
            )
            fmt = f" format={f.format}" if f.format else ""
            lines.append(f"- {f.field}:{fmt}{ex}")

    if ctx.destructive_actions:
        lines.append(
            "\nDestructive actions: " + ", ".join(ctx.destructive_actions)
        )

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text
