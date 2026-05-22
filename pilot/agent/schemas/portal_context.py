"""Portal context schema.

Hand-authored YAML file per portal that gives the planner grounding
about the portal's domain, page map, field conventions, and risky
actions. One file per portal at ``portals/<portal_id>/context.yaml``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class WaitPolicy(BaseModel):
    """F-08c: per-portal default wait timeouts.

    Replaces the prior pattern of hardcoded class-level defaults on
    StepEffect / NetworkExpectation / DomExpectation / StepAssertion.
    The annotator reads these values when generating expected_signals
    and assertions so the schema's class-level defaults are only ever
    a last-resort fallback for legacy skills.

    Tune upward on slow / queue-backed portals (the recorded ingest
    pipeline takes a while to settle), downward on snappy portals to
    fail fast and let recovery hooks kick in.
    """

    network_max_ms: int = 5000
    """Default max wait for a network expectation. Equivalent of the
    old NetworkExpectation.max_ms class default."""

    dom_timeout_ms: int = 5000
    """Default timeout for a DOM expectation."""

    dom_stable_ms: int = 250
    """Default stability window for options_changed / count_changed
    (the value must hold this long before we proceed -- prevents
    flicker)."""

    navigation_timeout_ms: int = 5000
    """Default timeout for NavigationEffect.assert_url verification."""

    assertion_timeout_ms: int = 4000
    """Default timeout for assert_after StepAssertion checks."""

    options_snapshot_max: int = 500
    """Cap on options captured per <select> snapshot (F-08b). When
    hit, ElementFingerprint.options_truncated=True so the annotator
    suggests a search step instead of treating the snapshot as
    exhaustive. The grabber reads this via window.__cp_opts_cap."""

    request_log_cap: int = 200
    """WI-09: cap on the in-page request log ring buffer used by the
    runner's expected_signals network wait. Default 200 -- larger than
    the legacy hardcoded LOG_CAP=50 because dashboards routinely fire
    >50 telemetry/API calls per page load and the target request
    could be evicted before the wait predicate runs. Tune upward for
    portals with heavy telemetry; downward for memory-constrained
    embedded browsers."""

    # ------------------------------------------------------------------
    # Followup #3: surface grabber.js hardcoded heuristics. Same pattern
    # as request_log_cap and options_snapshot_max -- the value is pushed
    # to a window.__cp_* global at watcher install; the grabber reads
    # the global with the literal as last-resort fallback.
    # ------------------------------------------------------------------

    input_debounce_ms: int = 400
    """Text-input debounce window inside the grabber. The grabber waits
    this long after the last keystroke before emitting one synthesized
    ``input_change`` event with the final value. Default 400 matches
    typical portal autocomplete debounce. Tune downward (e.g. 200) on
    snappy portals; upward (e.g. 700) on portals with heavy per-keystroke
    re-render so the grabber doesn't emit mid-typing intermediate values.
    Grabber reads via window.__cp_input_debounce_ms."""

    dom_mutation_burst_ms: int = 200
    """Debounce window for ``dom_mutation`` TraceEvent emission. The
    grabber's MutationObserver batches DOM changes that arrive within
    this window into one summary event so WI-09 / WI-10 can wait on
    "this action caused real DOM activity" without scanning every
    MutationRecord. Default 200. Increase on portals whose animations
    run >200ms (the burst summary will then capture the full animation
    rather than mid-animation noise). Grabber reads via
    window.__cp_dom_mutation_burst_ms."""

    attribution_fallback_window_ms: int = 50
    """History API attribution fallback budget (F-08a context). The
    synchronous attribution path uses an explicit ``activeInteraction``
    token thread-local to the current handler -- not a time window --
    so a pushState fired inside a click handler attributes correctly
    without any timing.

    This small window exists ONLY to cover the path where the History
    API wrapper itself uses ``setTimeout(0)`` to post the event: in that
    case the synchronous handler has already returned and the explicit
    token has been cleared by the microtask. The fallback keeps
    attribution alive for one event-loop tick so the wrapper's own
    setTimeout(0) can still attribute. This is NOT a 3-second guess like
    the original ATTRIBUTION_WINDOW_MS the audit flagged -- it's a
    single-tick budget that exists because of our own wrapper's
    setTimeout(0). Drop to 0 once History wrappers post directly.
    Grabber reads via window.__cp_attribution_fallback_ms."""

    hover_submenu_search_cap: int = 200
    """WI-40 hover-reveal submenu search cap. When the grabber detects
    a hover that revealed a submenu/mega-menu, it walks the trigger's
    parent subtree looking for the newly-visible container. This caps
    the number of nodes scanned so a deeply-nested portal layout
    doesn't stall the grabber. Default 200 covers typical menu depths;
    raise if a portal's mega-menu has a deep DOM subtree (>200 children
    in the trigger's container). Grabber reads via
    window.__cp_hover_submenu_search_cap."""

    page_snapshot_option_cap: int = 40
    """Per-``<select>`` option cap inside the page-catalog snapshot
    (the passive ``page_snapshot`` event the grabber emits after each
    navigation). NOT the same as ``options_snapshot_max`` -- this one
    bounds the JSON size of the catalog of every select on the page,
    whereas ``options_snapshot_max`` bounds the fingerprint snapshot
    for ONE interacted-with select. Default 40 keeps catalog payloads
    small. Raise if a portal's catalog needs richer per-select inventory
    for the planner. Grabber reads via window.__cp_page_snapshot_option_cap."""


class IdempotencyCapability(BaseModel):
    """How the portal's backend handles idempotency keys.

    Replaces the prior "inject Idempotency-Key into every non-GET
    request" behavior, which assumed backend semantics. Now opt-in
    per portal: the operator declares what the portal accepts.

    A portal that doesn't support idempotency keys leaves ``enabled``
    False (the default) and the runner won't inject the header.
    The runner still tracks per-step idempotency-context internally
    for retry safety -- it just doesn't send it on the wire."""

    # Followup #1: audit-critical schema -- idempotency drives
    # destructive request dedupe; a typo'd YAML key silently dropped
    # could land the runner in fail-open mode. extra="forbid" makes
    # YAML drift fail at portal-context load.
    model_config = ConfigDict(extra="forbid")

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

    scope_all_endpoints: bool = False
    """F-08d: explicit opt-in to inject on EVERY request matching
    ``method_patterns``. When ``enabled=True`` and ``endpoint_patterns``
    is empty, the validator requires this flag to be True -- otherwise
    the cap rejects the silent 'inject on every non-GET endpoint'
    behavior that the original WI-04 spec aimed to eliminate."""

    @model_validator(mode="after")
    def _validate_scope(self) -> "IdempotencyCapability":
        """F-08d: prevent the accidental 'inject on every endpoint'
        configuration. When the operator enables idempotency, they must
        either declare which endpoints accept the header
        (``endpoint_patterns``) or explicitly accept the all-endpoint
        scope (``scope_all_endpoints=True``). Silent fall-through to
        'inject everywhere' was the hidden default in WI-04 that this
        validator removes."""
        if self.enabled and not self.endpoint_patterns and not self.scope_all_endpoints:
            raise ValueError(
                "IdempotencyCapability.enabled=True requires either "
                "endpoint_patterns to be non-empty or "
                "scope_all_endpoints=True (explicit opt-in to inject "
                "the header on every method-matched request)."
            )
        return self


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

    wait_policy: WaitPolicy = Field(default_factory=WaitPolicy)
    """F-08c: per-portal wait timeout defaults. Annotator reads these
    when generating expected_signals / assertions so step-level
    expectations no longer carry hardcoded magic numbers."""

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
