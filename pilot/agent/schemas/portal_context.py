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


class IdempotencyCapability(BaseModel):
    """How the portal's backend handles idempotency keys.

    Replaces the prior "inject Idempotency-Key into every non-GET
    request" behavior, which assumed backend semantics. Now opt-in
    per portal: the operator declares what the portal accepts.

    A portal that doesn't support idempotency keys leaves ``enabled``
    False (the default) and the runner won't inject the header.
    The runner still tracks per-step idempotency-context internally
    for retry safety -- it just doesn't send it on the wire."""

    enabled: bool = False
    """Master switch. False -> runner does not inject the header. The
    audit log still records when a destructive step ran without
    configured idempotency, so the operator sees the gap."""

    header_name: str = "Idempotency-Key"
    """Header name the portal accepts. Some portals use ``X-Idempotency-Key``
    or vendor-specific names."""

    endpoint_patterns: list[str] = Field(default_factory=list)
    """URL substring patterns where injection applies. Empty list
    means "all non-GET requests get a key" (per ``method_patterns``).
    Use this to scope injection to known-safe destructive endpoints
    (e.g. ``/api/assets/`` + ``/api/orders/``) and skip injection on
    auth / search / autocomplete endpoints that don't dedupe."""

    method_patterns: list[Literal["POST", "PUT", "PATCH", "DELETE"]] = Field(
        default_factory=lambda: ["POST", "PUT", "PATCH", "DELETE"]
    )
    """HTTP methods to inject on."""

    key_components: list[Literal[
        "session", "step_index", "method", "url_path", "body_hash",
        "query_canonical",
    ]] = Field(
        default_factory=lambda: ["session", "step_index", "url_path"]
    )
    """Which components contribute to the key. Same components in the
    same order produce the same key, so a retry of the same logical
    operation dedupes. Adding ``body_hash`` makes the key depend on
    the request body -- distinct PATCHes against the same URL get
    distinct keys, preventing accidental dedupe of legitimate edits."""

    body_hash_fields: list[str] = Field(default_factory=list)
    """When ``key_components`` includes ``body_hash``, hash ONLY these
    fields (case-insensitive, dot-path on JSON bodies). Empty -> hash
    the whole body. Used to exclude noisy fields like timestamps
    while still differentiating semantically distinct requests."""

    allow_existing_header: bool = True
    """If the app code already sets an ``Idempotency-Key`` header on
    a request, leave it alone (default). Set False to force the
    runner's key to win -- needed when the app generates a fresh key
    per call and you want retries to dedupe against the runner's key
    instead."""


class AuthSignal(BaseModel):
    """How to tell whether the portal is currently authenticated.

    The pre-flight phase visits ``base_url`` and looks for any of
    ``logged_in_when_visible`` (positive signals) AND/OR the absence of
    ``logged_out_when_visible`` (negative signals). Either is enough on
    its own; the operator picks the one their portal exposes.
    """

    logged_in_when_visible: list[str] = Field(
        default_factory=list,
        description=(
            "CSS selectors / testids that appear ONLY when authenticated, "
            "e.g. ['[data-testid=\"nav-user-menu\"]', '#sign-out-btn']."
        ),
    )
    logged_out_when_visible: list[str] = Field(
        default_factory=list,
        description=(
            "Selectors that appear ONLY when NOT authenticated, e.g. "
            "['form#login', '[data-testid=\"sign-in-btn\"]']."
        ),
    )
    probe_timeout_ms: int = 5000


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
    auth_signal: AuthSignal = Field(default_factory=AuthSignal)
    """How pre-flight detects whether the portal is authenticated.
    Empty (default) means skip the auth probe -- portals without an
    explicit signal fall back to "trust the operator launched a logged-
    in tab"."""

    external_llm_enabled: bool = True
    """Per-portal kill switch for cloud LLM calls (intake / planner /
    reporter / annotate / future vision). Defaults True since this is
    a single-tenant local product today; set False when running against
    portals whose data cannot leave the operator's machine. The
    orchestrator audit-logs every external call regardless."""

    network_ignore: list[str] = Field(default_factory=list)
    """URL substring patterns the runner's expected_signals wait should
    *ignore* when computing 'is the network busy?'. Portals with
    long-poll notifications, SSE, or WebSocket channels keep at least
    one fetch in flight forever; without this list, every wait would
    burn the full timeout. Match is a simple substring check
    (case-insensitive) -- e.g. ``/api/notifications`` matches both
    ``/api/notifications?since=0`` and ``/api/notifications/stream``."""

    network_quiet_ms: int = 250
    """How long network has to be quiet (no non-ignored request
    finishing) before the in-flight predicate returns true. Tune
    upward for portals where requests dispatch in rapid bursts (UI
    fires four GETs serially after a click) and we want to wait for
    all of them. Tune downward for snappy portals."""

    idempotency: "IdempotencyCapability" = Field(
        default_factory=lambda: IdempotencyCapability()
    )
    """How the runner should inject idempotency keys on state-changing
    requests. Default is ``enabled=False`` -- portals opt in by
    declaring the capability, which is safer than the previous
    global-injection behavior (WI-04 from the fix-everything plan)."""

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
