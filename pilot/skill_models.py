"""Skill schema — a superset of Puppeteer Replay's JSON format.

A captured Skill is a portable, inspectable JSON describing an operator
demonstration of a portal task. Each step carries a fat element
fingerprint (multiple locator alternatives + accessibility context), the
action, optional parameter bindings, and optional post-conditions.

Design principle: every field that Puppeteer Replay understands stays
where it expects it, so a skill can be down-converted to plain Replay
JSON with zero transformation loss on the interaction basics. Our
extras (fingerprint alternatives, semanticLabel, paramBinding,
requiresGate, postCondition) live alongside without conflicting.

See: https://github.com/puppeteer/replay
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


ActionType = Literal[
    # Legacy v1 actions -- continue to work without changes
    "navigate",
    "click",
    "change",         # form input value set (debounced typing, select, etc.)
    "submit",
    "key",            # standalone key press (Enter, Escape) not tied to a change
    "upload",         # file input selection
    "wait",           # explicit wait / sleep
    "assert",         # post-condition assertion
    "set_selection",  # multi-select reconciliation (replace/add/remove items in a picker)
    # WI-01: structured action types. Schema accepts them now; runner
    # dispatches to per-action handlers as each subsequent WI lands.
    # Unimplemented handlers fail loudly with error_kind="action_not_implemented"
    # so the operator sees exactly which WI is pending instead of a
    # silent no-op.
    "fill_submit",          # text input burst + Enter/form submit, collapsed to one step (WI-15)
    "select_option",        # native single-select with declared enum aliases (WI-17)
    "select_autocomplete",  # query input + result selection from response container (WI-16)
    "date_select",          # native or custom calendar date picker (WI-21)
    "slider_set",           # range slider final value + event dispatch (WI-28)
    "drag_drop",            # pointer drag with DataTransfer semantics (WI-30)
    "toggle_state",         # accordion / expand-collapse desired state (WI-33)
    "modal",                # modal open/close with dialog visibility assertion (WI-34)
    "popup",                # window.open / target=_blank popup workflow (WI-35)
    "download",             # click + browser download capture (WI-45)
    "scroll_until",         # scroll a container until a target is visible (WI-38)
    "rich_text_set",        # contenteditable / rich-text editor content set (WI-39)
    "shortcut",             # global keyboard shortcut (Ctrl+S etc.) (WI-41)
    "canvas_gesture",       # canvas/SVG/media adapter-driven gesture (WI-49)
]


class OptionSnapshot(BaseModel):
    """One option captured from a select/listbox at record time."""

    # Followup #1: audit-critical schema -- producer (grabber) and consumer
    # (runner/annotator) must agree on the exact shape. extra="forbid" so
    # any silent kwarg drift (e.g. an old key the JS still writes) fails
    # at construction instead of being silently dropped, which is exactly
    # how F-01 went undetected for so long.
    model_config = ConfigDict(extra="forbid")

    value: str
    label: str
    selected: bool = False
    disabled: bool = False


class FrameStep(BaseModel):
    """WI-32: one step in a fingerprint's outer->inner frame chain.

    The chain describes how to reach an element that lives inside an
    iframe AND/OR a shadow root. The runner walks the chain in order:

      kind='iframe' + selector='iframe[name="preview"]'
        -> page.frame_locator('iframe[name="preview"]') ...
      kind='shadow' + host_selector='.my-design-system-host'
        -> element.evaluateHandle('h => h.shadowRoot') after locating
           the host inside the current scope.

    Either ``selector`` or ``host_selector`` is set depending on
    ``kind`` -- they're separate fields rather than one polymorphic
    field so the schema is unambiguous and JSON-introspectable.
    """

    kind: Literal["iframe", "shadow"]
    selector: Optional[str] = None
    """CSS selector when kind=iframe -- the iframe element's selector
    in the current scope. None when kind=shadow."""
    host_selector: Optional[str] = None
    """CSS selector when kind=shadow -- the shadow host element's
    selector in the current scope. None when kind=iframe."""

    @model_validator(mode="after")
    def _check_kind_field_consistency(self) -> "FrameStep":
        """Enforce: iframe steps carry selector, shadow steps carry
        host_selector. A FrameStep with the wrong field set is a
        recording / annotator bug and would silently fail at replay."""
        if self.kind == "iframe":
            if not self.selector:
                raise ValueError(
                    "FrameStep(kind='iframe') requires selector"
                )
        if self.kind == "shadow":
            if not self.host_selector:
                raise ValueError(
                    "FrameStep(kind='shadow') requires host_selector"
                )
        return self


class ElementFingerprint(BaseModel):
    """Fat fingerprint of a DOM element, captured at teach time.

    Replay uses each field as a fallback locator in priority order.
    Having all of them increases resilience to portal drift.
    """

    # Stable attributes — highest priority for Level 1
    test_id: Optional[str] = None           # data-testid
    element_id: Optional[str] = None        # id=
    name: Optional[str] = None              # name=
    aria_label: Optional[str] = None
    role: Optional[str] = None              # computed or explicit role

    # Semantic / text
    accessible_name: Optional[str] = None   # computed per WAI-ARIA
    text: Optional[str] = None              # trimmed innerText
    placeholder: Optional[str] = None
    tag: Optional[str] = None
    input_type: Optional[str] = None        # for <input type=...>

    # Structural fallbacks
    css_path: Optional[str] = None
    xpath: Optional[str] = None
    ancestor_chain: list[dict[str, Any]] = Field(default_factory=list)

    # Context for Level 3 (LLM-assisted) prompts
    landmark: Optional[str] = None          # nearest dialog/section/region name
    nth_of_role: Optional[int] = None       # "2nd button with role=button"
    bbox: Optional[dict[str, float]] = None  # x, y, width, height

    # Frame / shadow path (empty = top document). Legacy boolean +
    # string-list shape is preserved for back-compat with pre-WI-32
    # recordings. WI-32 introduces the richer ``frame_chain`` and
    # ``shadow_path`` fields so the runner can traverse iframes (via
    # page.frame_locator) and shadow roots (via evaluate handles)
    # before resolving the target locator.
    frame_path: list[str] = Field(default_factory=list)
    """Legacy: outer->inner iframe selectors. New recordings populate
    ``frame_chain`` with richer kind-tagged steps; ``frame_path`` is
    derived from frame_chain for back-compat with pre-WI-32 runner
    code paths."""
    in_shadow_root: bool = False
    """Legacy boolean. WI-32 replaces with ``shadow_path``. Set True
    when the element lived inside ANY shadow root, regardless of host
    chain depth. Pre-WI-32 recordings only set this flag; the runner
    can't resolve those without re-recording."""
    frame_chain: list["FrameStep"] = Field(default_factory=list)
    """WI-32: outer->inner frame traversal path. Each step is a
    FrameStep describing whether to enter an iframe (via
    page.frame_locator(selector)) or pierce a shadow root (via
    element.evaluateHandle for getRootNode().host). Empty = top
    document, no traversal. The grabber walks up from the target
    element through document.defaultView.frameElement (iframe
    boundaries) and getRootNode().host (shadow boundaries) to build
    this list."""
    shadow_path: list[str] = Field(default_factory=list)
    """WI-32: ordered list of shadow-host CSS selectors. Empty = no
    shadow root involvement. When the target lives inside a shadow
    tree, each entry is the host element's selector relative to its
    own document (the runner walks each entry through evaluate, then
    descends to the next). Mostly redundant with ``frame_chain`` (a
    shadow step in frame_chain carries the same host selector); kept
    for back-compat + a simple boolean-replacement when the operator
    only needs to know 'is this in a shadow root.'"""

    # WI-03: richer control metadata captured at record time. These let
    # the annotator pick the right semantic action (select_option,
    # date_select, slider_set, rich_text_set, ...) and let the runner
    # know the control's contract without guessing from the DOM at
    # replay (when state may differ from recording). Each is optional
    # and Pydantic defaults to None / False for legacy fingerprints.
    control_kind: Optional[Literal[
        "button", "link", "text_input", "password_input", "number_input",
        "email_input", "url_input", "search_input", "tel_input",
        "textarea", "select_single", "select_multiple", "combobox_aria",
        "listbox_aria", "checkbox", "radio", "date_input", "time_input",
        "datetime_input", "month_input", "week_input", "color_input",
        "range_slider", "file_input", "contenteditable", "anchor",
        "tab", "menuitem", "treeitem", "option", "unknown",
    ]] = None
    """Semantic kind of control. ``text_input`` covers free-text; the
    specific input_type subtypes are kept for codec routing (date vs
    number vs email). ``select_multiple`` distinguishes from
    ``select_single`` so the annotator emits set_selection vs
    select_option. ``combobox_aria`` and ``listbox_aria`` cover custom
    widgets implementing the WAI-ARIA combobox/listbox patterns."""
    value_kind: Optional[Literal[
        "string", "number", "boolean", "date", "datetime", "time",
        "color", "file", "list", "html", "none",
    ]] = None
    """Type of value the control yields. ``html`` is for
    contenteditable / rich-text editors. ``none`` is for clickable
    elements that don't carry a value (buttons, links)."""
    options_snapshot: Optional[list[OptionSnapshot]] = None
    """For selects, listboxes, comboboxes: every option's value + label
    + selected flag at the moment of recording. The annotator uses
    this to emit declared aliases (WI-25) so replay doesn't fuzzy-
    match wrong locale/status options. Each entry: ``{value, label,
    selected?, disabled?}``."""
    selected_options: Optional[list[str]] = None
    """Currently-selected option values at record time. For
    select_multiple / combobox, may carry multiple. For
    select_single, one entry. Used by the annotator to bind the param
    value back to a specific option."""
    aria_expanded: Optional[bool] = None
    aria_disabled: Optional[bool] = None
    aria_busy: Optional[bool] = None
    """ARIA state flags. ``aria_expanded`` is critical for accordions /
    expand-collapse / combobox open-state (WI-33). ``aria_disabled``
    and ``aria_busy`` feed readiness-aware waits (WI-43)."""
    disabled: Optional[bool] = None
    readonly: Optional[bool] = None
    """Native HTML attribute equivalents. The runner waits for
    disabled-to-enabled transitions (WI-43) before interacting."""
    contenteditable: Optional[bool] = None
    """True if the element (or an ancestor) has contenteditable. Routes
    the annotator to emit rich_text_set actions (WI-39) instead of
    treating typing as plain input_change."""
    locale_hint: Optional[str] = None
    timezone_hint: Optional[str] = None
    """Page-level locale + timezone snapshot. The annotator uses these
    to normalize date / number values to canonical form (WI-48) so
    a date recorded under en-US doesn't get reinterpreted under
    en-GB at replay."""
    min: Optional[str] = None
    max: Optional[str] = None
    step: Optional[str] = None
    """Numeric/range/date constraints from the HTML attributes. Used
    by slider_set (WI-28) and date_select (WI-21) to validate replay
    values before page mutation."""
    accept: Optional[str] = None
    """For file inputs: the accept attribute. Lets the runner validate
    file MIME / extension before upload (WI-29)."""
    multiple: Optional[bool] = None
    """For file inputs: ``multiple`` attribute. For selects: indicates
    set semantics."""
    options_truncated: Optional[bool] = None
    """F-08b: True when ``options_snapshot`` hit the configured cap
    (PortalContext.options_snapshot_max, default 500). The annotator
    treats a truncated snapshot as 'do not assume this is exhaustive'
    -- emit a search/filter step (WI-25) instead of declared aliases."""

    # 2026-06-02 diagnosis B3: the human-readable display value of the
    # widget at recording time. For mat-select this is the trimmed text
    # of ``.mat-select-value-text`` (e.g. "18_KANTM2_8K"); for
    # mat-checkbox the boolean string "true"/"false"; for
    # ng-multiselect-dropdown the joined selected-chip text. null when
    # the widget exposes no steady-state value (plain buttons / links).
    # Critical disambiguator for the planner when two structurally
    # identical mat-selects (same tag, same null role/name post-B1+B2)
    # nevertheless show distinct on-screen values -- e.g. a Year
    # dropdown showing "2019" vs a Model dropdown showing "18_KANTM2_8K".
    current_value: Optional[str] = None

    # Alternate fingerprints accumulated by self-heal (L3) over time.
    # On replay, each alternate is tried via L1/L2 BEFORE invoking L3
    # again, so a portal that drifted once stays cheap to re-execute.
    # Persisted back to the skill JSON when a heal succeeds + verifies.
    alternates: list["ElementFingerprint"] = Field(default_factory=list)

    # Param-templated fields. Keys are fingerprint field names that
    # contain a parameter value as a substring (test_id, element_id,
    # css_path, xpath, accessible_name, text). Values are template
    # strings with ``{param_name}`` placeholders. Discovered at annotate
    # time by scanning the literal fingerprint for matches against
    # recorded param values, then substituted at replay time so a
    # recording with testid="row-A-9001" replays correctly when
    # content_id="B-12345" yields testid="row-B-12345".
    templates: dict[str, str] = Field(default_factory=dict)

    # WI-11: per-template-field provenance source. Keys mirror ``templates``;
    # values describe HOW the template was derived. Sources:
    #   ``row_key``         -- the templated value came from an ancestor's
    #                          data-row-key / data-testid="...row-{X}" / id="...-{X}"
    #                          attribute (the row the click target lived in).
    #                          Unambiguous and the strongest provenance.
    #   ``route_param``     -- value matches a URL path segment captured at
    #                          interaction time (used by NavigationEffect).
    #   ``request_param``   -- value appeared in a captured request's query
    #                          string or body field.
    #   ``selected_option`` -- value matches a recorded options_snapshot
    #                          entry's value/label.
    #   ``operator_input``  -- value matches what the operator typed into
    #                          this very step's input field (binding value).
    #                          Safe ONLY when the param is bound to the
    #                          element with the templated field.
    #   ``legacy_substring`` -- value substring-matched the recorded literal
    #                          but no stronger provenance was found. Kept
    #                          for back-compat; new annotations should
    #                          prefer the stronger sources.
    # When the field isn't in this map, treat as ``legacy_substring`` for
    # back-compat with skills written before WI-11.
    template_sources: dict[str, Literal[
        "row_key",
        "route_param",
        "request_param",
        "selected_option",
        "operator_input",
        "legacy_substring",
    ]] = Field(default_factory=dict)

    # WI-11: structured template segments parallel to ``templates``. Keys
    # are field names (same set as ``templates``); values are ordered
    # lists of TemplatePart describing the literal + placeholder anatomy.
    # Populated by _derive_provenance_templates; readable by future
    # analyzers without re-parsing the raw string form. Empty for legacy
    # skills (the raw ``templates`` field remains the source of truth for
    # the runner's _materialize_fingerprint call).
    template_parts: dict[str, list["TemplatePart"]] = Field(default_factory=dict)


class TemplatePart(BaseModel):
    """WI-11: structured template segment.

    A template like ``catalog-row-{asset_id}`` decomposes into two parts:
    ``[{kind="literal", text="catalog-row-"}, {kind="placeholder",
    param="asset_id"}]``. Carrying the structured form alongside the
    raw template string lets the runner (and future analyzers) reason
    about template anatomy without re-parsing strings -- e.g. a
    locator scorer can weight placeholder boundaries higher than
    literal chars.

    The legacy raw-string ``templates`` field on ElementFingerprint
    remains for back-compat; ``template_parts`` is the additive
    structured representation populated by the WI-11 annotator. Both
    are written together so old consumers keep working unchanged.
    """

    # Followup #1: audit-critical schema. The matrix test silently passed
    # for weeks with ``TemplatePart(kind="literal", value="btn-open-")``
    # because Pydantic dropped ``value`` and ``text`` stayed None. With
    # extra="forbid", the same drift fails loud at construction.
    model_config = ConfigDict(extra="forbid")

    kind: Literal["literal", "placeholder"]
    text: Optional[str] = None
    """For kind=literal: the verbatim text segment. None for placeholders."""
    param: Optional[str] = None
    """For kind=placeholder: the param name (without braces). None for
    literals."""


class ParamBinding(BaseModel):
    """Binds a step's value (or a substring of it) to a named parameter."""

    name: str                               # e.g. "content_id"
    type: Literal["string", "number", "date", "file_path"] = "string"
    mode: Literal["whole", "substring", "template"] = "whole"
    template: Optional[str] = None          # when mode=template, e.g. "{{content_id}}_hero.jpg"


class PostCondition(BaseModel):
    """A check to run after the action — element appears, text matches, etc."""

    kind: Literal["element_appears", "element_disappears", "text_contains", "url_changes"]
    fingerprint: Optional[ElementFingerprint] = None
    expected_text: Optional[str] = None
    timeout_ms: int = 5000


class StepAssertion(BaseModel):
    """Declarative post-condition checked after the step's action runs.

    Lightweight and action-specific by design. The runner verifies each
    assertion in order; the first failure flips the step to failed with
    error_kind="post_condition_failed" and recovery kicks in.

    The expected_signals field on the step handles WAITING; this field
    handles VERIFYING. Keep them separate -- one is "be patient", the
    other is "we are at the right page state."
    """

    kind: Literal[
        "visible",
        "hidden",
        "count_eq",
        "count_gte",
        "count_lte",
        "text_contains",
        "url_contains",
        "attr_equals",
        # WI-27: action-specific postcondition kinds. Each one is an
        # action's structural success signal that the L3 verifier can
        # check instead of the legacy whole-page-signature
        # (url + body innerText length + interactable count) which
        # the audit flagged as the wrong signal -- a spinner text
        # change passes a wrong click, a silent save fails verification.
        "url_matches_template",   # nav: post-action URL fits a template
        "field_value_equals",     # form: input/textarea reads a value
        "selection_equals",       # select_option: <select>.value matches
        "toast_visible",          # save / conflict / undo toast appeared
        "request_completed",      # specific network call settled with status
        "download_started",       # browser download trigger fired
        # WI-44: server validation error bound to a specific form
        # field. The runner reads the field's [aria-invalid] / nearby
        # role=alert text and surfaces the validation message in
        # error_details. ``validation_field`` is the assertion kind
        # used when the operator EXPECTS validation to fire (negative
        # path testing). When validation fires UNEXPECTEDLY, the
        # runner uses the auto-detect path below in
        # _check_validation_errors -- no assertion required.
        "validation_field",
    ]
    selector: Optional[str] = None
    """CSS selector. Required for visible / hidden / count_* /
    attr_equals / field_value_equals / selection_equals / toast_visible."""
    n: Optional[int] = None
    """For count_* kinds: the expected count."""
    text: Optional[str] = None
    """For text_contains: substring expected somewhere on the page or
    inside ``selector`` if provided. For url_contains: substring expected
    in the current URL. For url_matches_template /
    field_value_equals / selection_equals / toast_visible: the expected
    value (template / canonical value / message substring)."""
    attr: Optional[str] = None
    """For attr_equals: attribute name."""
    timeout_ms: int = 4000
    """F-08c: 4000 is a legacy fallback. New annotations should set
    this explicitly from PortalContext.wait_policy.assertion_timeout_ms."""
    url_template: Optional[str] = None
    """WI-27: for kind=url_matches_template, the expected URL template
    (e.g. ``/asset/{content_id}``) the post-action URL must match
    once params are rendered."""
    request_pattern: Optional[str] = None
    """WI-27: for kind=request_completed, the URL substring of the
    request the action's L3 verifier must observe a completion for."""
    expected_status: Optional[int] = None
    """WI-27: for kind=request_completed, the response status the
    matched request must carry. None means "any 2xx" (200-299)."""
    filename_pattern: Optional[str] = None
    """WI-27: for kind=download_started, optional filename substring
    or template fragment the download must match."""

    # WI-44: validation_field assertion fields.
    validation_field_id: Optional[str] = None
    """WI-44: for kind=validation_field: the form-field's identifier
    (test_id / id / name). The runner resolves the field via this id
    and reads its [aria-invalid] attribute + nearby [role='alert'] /
    error-text element to extract the validation message."""
    validation_level: Optional[Literal["error", "warning"]] = None
    """WI-44: severity the assertion expects. ``error`` means the
    field MUST carry aria-invalid='true' AND a non-empty validation
    message; ``warning`` accepts the validation message without
    aria-invalid being set (warning-level annotations often live as
    a sibling [role='status'] without flagging the field itself)."""
    message_pattern: Optional[str] = None
    """WI-44: for kind=validation_field: substring the validation
    message must contain (case-insensitive). When None, any non-empty
    message satisfies the assertion."""


class NetworkExpectation(BaseModel):
    """One network call the runner should wait for after the action.

    URL match is a case-insensitive substring (NOT regex) so portal
    URLs with query params are still easy to match. ``optional`` means
    "wait if it appears within max_ms, but don't fail if it doesn't" --
    used for stale GET requests that may be cached at replay.
    """

    # Followup #1: audit-critical schema -- the matrix test passed for
    # weeks with ``NetworkExpectation(match_mode="started_after", ...)``
    # because Pydantic dropped ``match_mode`` silently. extra="forbid"
    # blocks the next silent drift at construction.
    model_config = ConfigDict(extra="forbid")

    url_pattern: str
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] = "GET"
    status: Optional[int] = None
    """Expected status code. If set, the call only counts as 'completed
    correctly' when this status is observed. None means require any
    2xx -- the runner predicate enforces ``200 <= status < 300``
    (F-09c), matching this docstring; older runner behavior of
    accepting any status was a bug."""
    max_ms: int = 5000
    """F-08c: 5000 is a legacy fallback. New annotations should set
    this explicitly from PortalContext.wait_policy.network_max_ms."""
    optional: bool = False
    started_after_event: Optional[str] = None
    """F-09: event_id of a baseline event the matched request must
    have started AFTER. Without this, a stale prior request can
    accidentally satisfy a later step's expected_signal. None = no
    baseline (pre-F-09 behavior)."""


class DomExpectation(BaseModel):
    """One DOM condition the runner should wait for after the action."""

    kind: Literal[
        "visible",
        "hidden",
        "options_changed",  # for cascading dropdowns: the option list mutated
        "count_changed",
        # WI-10: observed-readiness kinds. Annotator emits these when the
        # grabber's WI-10 readiness watcher captured the corresponding
        # transition DURING the originating action's effect window. The
        # runner waits for the declared signal; the legacy
        # _SPINNER_SELECTOR becomes a last-resort fallback only when no
        # readiness signals are declared on the step.
        "aria_busy",                # aria-busy on target became "false"
        "role_progressbar_hidden",  # role=progressbar inside scope disappeared
        "disabled_until_enabled",   # disabled attr cleared on target
        "field_enabled",            # field's :disabled flipped to false
        "text_transition",          # innerText of target changed to ``text``
        "selector_hidden",          # arbitrary selector reached display:none
        # WI-38: target visibility inside a scroller. ``selector`` carries
        # the scroller itself; ``text`` carries the target's selector (so
        # the runner can re-probe the target's visibility inside the
        # scroller's viewport). Used by scroll_until to declare the
        # termination condition without hardcoding "N scrolls."
        "target_visible_in_scroller",
    ]
    selector: str
    stable_ms: int = 250
    """How long the condition must hold before we proceed. Prevents
    flicker -- a dropdown that briefly empties then refills shouldn't
    falsely satisfy options_changed. F-08c: 250 is a legacy fallback;
    new annotations should set explicitly from
    PortalContext.wait_policy.dom_stable_ms."""
    timeout_ms: int = 5000
    """F-08c: 5000 is a legacy fallback. New annotations should set
    this explicitly from PortalContext.wait_policy.dom_timeout_ms."""
    text: Optional[str] = None
    """WI-10: for ``text_transition`` kind, the target text the
    selector's innerText should match (substring, case-insensitive).
    e.g. a Save button transitions from 'Saving...' back to 'Save'
    when the request resolves -- annotator emits text='Save'."""


class PushExpectation(BaseModel):
    """WI-47: a WebSocket / SSE / push-notification message the runner
    waits for after an action.

    Models the pattern: operator triggers an async job (e.g. clicks
    "Export") and the portal delivers the completion signal through a
    push channel (e.g. WebSocket frame with ``{type: 'job.completed'}``
    or SSE ``event: complete``). The legacy runner couldn't wait on
    this -- it would either poll the DOM (race-prone) or fall back to
    a fixed sleep.

    The grabber captures WebSocket / EventSource open + message + close
    as TraceEvents (kind=``network_request`` with method=``WS`` /
    ``SSE`` and the frame summary in ``mutation_summary``). The runner
    inspects the page-side request log for a matching frame.

    Matching contract:
      - ``channel`` substring-matches against the WS/SSE channel URL
        (case-insensitive). Required.
      - ``type`` substring-matches against the message body's ``type``
        / ``event`` field as captured in mutation_summary. Optional --
        absence means any message on the channel satisfies.
      - ``payload_match`` substring-matches against the message body
        summary captured in mutation_summary.body_summary. Optional.
    """

    channel: str
    """Channel URL substring (case-insensitive). For WebSockets the
    grabber records the constructor URL; for SSE the EventSource URL.
    Substring keeps it tolerant of session IDs / cache busters."""
    type: Optional[str] = None
    """Optional message type / event-name substring. Examples:
    ``job.completed``, ``ready``. None matches any frame."""
    payload_match: Optional[str] = None
    """Optional payload body substring (case-insensitive). Matched
    against the captured frame body summary. None disables payload
    matching."""
    max_ms: int = 15000
    """Max wait for the matching message. Default 15000ms because
    server-side jobs can take several seconds before pushing a
    completion frame; the operator should override this per-portal
    via PortalContext.wait_policy when the workload's latency is
    known."""


class ExpectedSignals(BaseModel):
    """Per-step hints the runner uses to wait *only when needed*.

    Generic in-flight network counting fails on portals that have
    long-poll / SSE channels (in-flight is never 0). Per-step
    expected_signals lets the recording teach the runner exactly what
    to wait for -- e.g. "after Region click, wait for
    /api/markets?region=* to complete AND the market dropdown to
    refresh its options."
    """

    network: list[NetworkExpectation] = Field(default_factory=list)
    dom: list[DomExpectation] = Field(default_factory=list)
    push: list[PushExpectation] = Field(default_factory=list)
    """WI-47: WebSocket / SSE / push-notification frames the runner
    waits for AFTER the action. Empty for non-push workflows. The
    runner inspects the page-side __cp_request_log for entries whose
    method is ``WS`` / ``SSE`` and whose URL + body summary match
    the declared expectation."""


# ---------------------------------------------------------------------------
# WI-01: StepEffect family
#
# Effects model the consequences of an action. Today's recordings treat
# every consequence as a separate independent step ("click", then a
# standalone "navigate", then a standalone "modal-appear"). That loses
# causality and produces the click-then-hardcoded-navigate bug from the
# fix-everything-plan audit (BLOCKER 1).
#
# Going forward, the annotator folds caused consequences into the
# causing step's ``effects`` field. The runner asserts the effect after
# executing the action -- it does not re-execute the consequence.
#
# Each effect subtype is optional. A click that triggers navigation
# would have ``effects.navigation`` populated; the standalone
# ``navigate`` step that today's recorder emits would be either dropped
# (it's the same action) or rewritten as a click-with-effect.
# ---------------------------------------------------------------------------


class NavigationEffect(BaseModel):
    """An action that caused (or is expected to cause) a URL change."""

    kind: Literal[
        "spa_route",        # history.pushState / popstate / hashchange
        "full_document",    # full document navigation
        "hash",             # hash-only navigation
        "history_replace",  # history.replaceState
        "manual",           # operator typed in address bar (rare)
    ]
    url: Optional[str] = None
    """Literal URL observed at recording. Kept verbatim for audit, but
    NOT used as the replay target -- use ``url_template`` if you want
    to navigate to a parameterized URL."""
    url_template: Optional[str] = None
    """Templated URL with ``{param}`` placeholders, derived from
    provenance at annotate time. For verification only when used on a
    click effect -- runner asserts URL matches this template, never
    calls ``page.goto`` for caused navigation."""
    url_template_source: Optional[Literal[
        "route_param",
        "row_key",
        "operator_input",
        "legacy_substring",
    ]] = None
    """WI-11: how url_template was derived. ``route_param`` means the
    templated segment IS the URL path segment (the strongest provenance
    -- the navigation URL itself carries the value). ``row_key`` means a
    bound row key from a prior click. ``operator_input`` covers values
    from this skill's typed inputs. ``legacy_substring`` for pre-WI-11
    skills upgraded at load time; runner can choose to be more
    conservative (e.g. skip assertion) when the source is weak."""
    source: Optional[str] = None
    """How the navigation fired: ``history.pushState`` /
    ``history.replaceState`` / ``popstate`` / ``hashchange`` /
    ``anchor`` / ``form_submit`` / ``location_assign`` / ``manual``."""
    reload_allowed: bool = False
    """If True, runner is allowed to call ``page.goto`` to force the
    URL. Default False: caused navigations must come from the action,
    not from a redundant goto."""
    assert_url: Optional[str] = None
    """Optional regex or substring the post-action URL must match.
    Default: derived from ``url_template`` if absent."""
    timeout_ms: int = 5000
    """F-08c: 5000 is a legacy fallback. New annotations should set
    this explicitly from
    PortalContext.wait_policy.navigation_timeout_ms."""


class PopupEffect(BaseModel):
    """An action that opens a new tab / window / popup."""

    url_template: Optional[str] = None
    window_name: Optional[str] = None
    page_binding_key: Optional[str] = None
    """Key the runner uses to register the new page in its page
    registry. Subsequent steps targeting this page bind by key."""
    switch_policy: Literal["switch", "stay"] = "switch"
    """``switch``: subsequent steps execute on the popup until an
    explicit context-switch step. ``stay``: popup opens but the
    original page remains active."""
    close_policy: Literal["explicit", "auto"] = "explicit"


class DownloadEffect(BaseModel):
    """An action that triggers a browser download."""

    filename_template: Optional[str] = None
    mime: Optional[str] = None
    save_policy: Literal["session_artifact", "user_path"] = "session_artifact"
    path_param: Optional[str] = None
    """Skill param holding the target save path when save_policy is
    ``user_path``."""


class ModalCloseAction(BaseModel):
    """WI-34: one mechanism by which a modal was closed during the
    recorded interaction window. The annotator captures every close
    mechanism observed (button click, Escape, backdrop click) so the
    runner can pick a stable one at replay -- a portal whose Close
    button gets a different testid still closes via Escape."""

    kind: Literal["click", "escape", "backdrop"]
    """``click`` -- the operator clicked a button/icon inside the
    dialog. ``target_fp`` carries that element's fingerprint.
    ``escape`` -- the operator pressed Escape while focused inside
    the dialog. No target. ``backdrop`` -- the operator clicked
    outside the dialog (on the backdrop / overlay). No target."""

    target_fp: Optional[ElementFingerprint] = None
    """For ``kind='click'``: fingerprint of the in-dialog close
    control. None for ``escape`` / ``backdrop`` kinds."""


class ModalEffect(BaseModel):
    """WI-34: an action that opens and/or closes a modal/dialog.

    A "confirm publish" workflow records as ONE step:
      - click Publish button (the step's primary action)
      - dialog appears (opens_on_action=True)
      - click Yes inside dialog (folded as a close_action of kind=click)
      - dialog disappears (closes_on_action=True)

    The runner waits for the dialog to be visible after the opening
    action, then for the close_actions to dismiss it, then verifies the
    dialog is gone. When the modal contains its OWN interactive steps
    (e.g. fill a form, click Submit), those steps live as separate
    SkillSteps; the runner scopes their locators to inside the dialog
    when ``opens_on_action=True`` is in effect on a prior step.

    Backward compatibility: the legacy ``kind="open"``/``"close"`` +
    ``dialog_selector`` shape pre-WI-34 is still accepted. The two
    new boolean fields ``opens_on_action`` / ``closes_on_action`` are
    the WI-34 contract; the runner prefers them when set and falls
    back to ``kind`` otherwise.
    """

    # WI-34 contract (preferred):
    opens_on_action: bool = False
    """True when the action causes the dialog to MOUNT (transition from
    not-present / hidden to visible). Runner waits for the dialog to
    become visible after the action."""

    closes_on_action: bool = False
    """True when the action causes the dialog to UNMOUNT (transition
    from visible to hidden / removed). Runner verifies the dialog is
    gone after the close_actions fire."""

    close_actions: list[ModalCloseAction] = Field(default_factory=list)
    """One or more close mechanisms folded into THIS step. When
    ``closes_on_action=True``, the runner picks the first viable
    close_action (click target visible -> use it; otherwise fall back
    to escape; backdrop is last). Empty when the close is purely a
    consequence of the action itself (e.g. submit click that auto-
    dismisses the dialog -- no extra interaction needed)."""

    # Legacy shape (pre-WI-34); kept for back-compat with skills built
    # before this WI landed. New annotations populate the WI-34 fields
    # above and leave ``kind`` None.
    kind: Optional[Literal["open", "close"]] = None
    dialog_test_id: Optional[str] = None
    dialog_selector: Optional[str] = None
    expected_visibility: Optional[bool] = None
    """Legacy field. After the action: True = dialog visible, False =
    dialog hidden. WI-34 prefers the two boolean fields above; this
    field is kept so pre-WI-34 skills still validate."""


class HoverEffect(BaseModel):
    """WI-40: a parent element the operator hovered to reveal a
    submenu, which then became visible and was clicked.

    Hover menus / mega menus / dropdown navigation are recorded as a
    sequence of pointerenter -> mouseover (parent) -> submenu DOM
    mutation -> click (child). The annotator collapses this into ONE
    click step with a HoverEffect attached that tells the runner:

      1. mouse.move to ``target_fp`` (the parent menu trigger),
      2. wait for ``opens_submenu_selector`` to become visible, then
      3. click the step's primary fingerprint (the child item).

    Dwell time captured at record is observational evidence only --
    the runner DOES NOT sleep for ``dwell_ms``. It waits for the
    declared visibility signal so a fast portal doesn't wait
    unnecessarily and a slow portal doesn't race the submenu render.

    Acceptance check (from the WI brief):
      A 'File > Save As' mega-menu click records and replays even
      though the submenu doesn't render until the parent is hovered.
    """

    target_fp: ElementFingerprint
    """Fingerprint of the parent menu trigger the operator hovered.
    The runner dispatches a synthetic mouse.move to this element's
    centroid before clicking the step's primary fingerprint."""

    dwell_ms: int = 0
    """Observed hover dwell at record time (ms). Audit-only; the
    runner uses the visibility signal as the gate, not this value.
    Kept on the spec so an operator can see what was originally
    needed during recording."""

    opens_submenu_selector: Optional[str] = None
    """CSS selector for the submenu container expected to become
    visible after the hover. The runner waits for this selector to
    reach display:not-none + visibility:visible before clicking the
    primary target. None means the runner just dispatches the hover
    and immediately clicks -- works for very fast portals but is the
    fragile fallback."""


class ToastEffect(BaseModel):
    """WI-42: a transient toast/snackbar that appeared after the action.

    Used for save-confirm toasts, conflict-resolution toasts, undo
    toasts. May carry an action button the workflow can click.

    Runner contract:
      - When ``level='error'`` the post-action verify FAILS the step
        with ``error_kind='toast_error'`` and ``text_pattern`` in
        error_details so the operator sees the validation message.
      - When ``level`` in (info, success, warning) the toast is
        treated as confirmation. The runner asserts visibility (when
        ``message_matcher`` is set) via the WI-27 ``toast_visible``
        assertion semantics.
      - ``dependent_actions`` describes Undo / Retry buttons the
        workflow may click as a follow-up. Today these surface as
        ambiguity-prompt-style pauses to the operator; future WIs
        will model 'click Undo on the toast' as its own step.
    """

    level: Optional[Literal["info", "success", "warning", "error"]] = None
    """WI-42: severity of the toast. ``error`` flips the step to
    failed; the others are confirmation/audit only. None means the
    grabber couldn't infer a level (no explicit class / role hint);
    the runner treats None as info."""

    text_pattern: Optional[str] = None
    """WI-42: substring the toast text must match for the assertion
    to succeed. Distinct from ``message_matcher`` only in name; both
    are kept for back-compat. Annotator populates text_pattern for
    new skills."""

    dismiss_strategy: Optional[Literal[
        "auto", "manual_close", "click_action",
    ]] = None
    """WI-42: how the toast disappears. ``auto`` -- it expires on its
    own; ``manual_close`` -- has an X / dismiss button the operator
    pressed during recording; ``click_action`` -- it had an action
    button (Undo / Retry) the operator may want to click. Drives the
    runner's post-toast wait."""

    dependent_actions: list[dict[str, Any]] = Field(default_factory=list)
    """WI-42: interactive controls (Undo, Retry, View Details) the
    toast carried. Each entry shape: {label, test_id, action_kind:
    'undo'|'retry'|'view'}. Audit-only today; future WIs may model
    'click the Undo' as a follow-up step."""

    message_matcher: Optional[str] = None
    """Pre-WI-42: substring/regex match for the toast text. Kept for
    back-compat with skills built before WI-42. New annotations
    populate text_pattern instead; the runner reads text_pattern
    first, falls back to message_matcher."""
    action_button_test_id: Optional[str] = None
    expiry_policy: Optional[Literal["auto", "manual", "click"]] = None
    conflict_kind: Optional[str] = None
    """``save_conflict`` | ``validation_error`` | ``info`` | ..."""


class NewTabEffect(BaseModel):
    """Less specific than PopupEffect -- the action opens a new tab via
    some mechanism (target=_blank, window.open, CDP page event)."""

    binding_key: str
    target_url_template: Optional[str] = None


class StateChangeEffect(BaseModel):
    """A targeted attribute/state mutation on a specific element. Used
    to describe accordion / expanded / disabled-to-enabled / aria-busy
    transitions caused by the action."""

    target_selector: str
    attribute: str
    from_value: Optional[str] = None
    to_value: Optional[str] = None


class StepEffect(BaseModel):
    """Container for all consequence types of an action.

    Multiple effect types can co-exist on one step (e.g. a click that
    opens a modal AND triggers a network call). Each field is optional
    and defaults None; the runner inspects only the populated ones."""

    navigation: Optional[NavigationEffect] = None
    popup: Optional[PopupEffect] = None
    download: Optional[DownloadEffect] = None
    modal: Optional[ModalEffect] = None
    toast: Optional[ToastEffect] = None
    new_tab: Optional[NewTabEffect] = None
    state_change: Optional[StateChangeEffect] = None
    hover: Optional["HoverEffect"] = None
    """WI-40: hover prerequisite for the action's primary target. When
    set, the runner moves the mouse to the hover target, waits for the
    declared opens_submenu_selector to appear, then performs the
    action. None = no hover prerequisite (the action's target is
    directly visible)."""
    network: list["NetworkExpectation"] = Field(default_factory=list)
    """Network calls the action is expected to cause. Different from the
    legacy step.expected_signals.network which gates a wait; these are
    declared as consequences of THIS action, scoped to the action's
    timeframe."""
    dom: list["DomExpectation"] = Field(default_factory=list)
    """DOM mutations the action is expected to cause."""


# ---------------------------------------------------------------------------
# WI-01: ReplayPolicy
#
# Default policy for steps is fail-fast (on_failure="abort"). Today's
# runner continues after a sub-step failure (BLOCKER 5 in the audit);
# that's wrong for state-mutating skills. Steps that are explicitly
# observational or expected-to-fail-sometimes can declare
# on_failure="continue" or "optional".
# ---------------------------------------------------------------------------


class ReplayPolicy(BaseModel):
    # Followup #1: audit-critical schema -- ReplayPolicy decides whether
    # the runner aborts or continues; an unknown kwarg silently dropped
    # could produce the opposite of the operator's intent. extra="forbid"
    # makes that drift class impossible.
    model_config = ConfigDict(extra="forbid")

    on_failure: Literal["abort", "continue", "optional", "recover"] = "abort"
    """``abort`` (default): runner stops the skill on this step's
    failure; orchestrator pause flow takes over. ``continue``:
    explicitly tolerate failure, log + proceed. ``optional``: same as
    continue but no error_kind emitted. ``recover``: hand off to the
    runner's recovery hooks (future WI-26)."""
    reload_allowed: bool = False
    """Permit ``page.goto`` to force the recorded URL if the action's
    natural behavior doesn't produce the expected navigation. Default
    False -- prevents accidental full SPA reloads that re-trigger the
    page's initial fetches."""
    requires_current_page: Optional[str] = None
    """URL pattern (substring) that must currently match before this
    step runs. Used by cross-tab workflows (WI-46) so a step bound to
    the preview tab doesn't accidentally run on the catalog tab."""
    optional: bool = False
    """Marks the step as observational. Failures don't propagate as
    skill failure. Convenience flag; equivalent to on_failure="optional"."""

    @model_validator(mode="after")
    def _normalize_optional(self) -> "ReplayPolicy":
        """F-09: ``optional=True`` and ``on_failure="abort"`` are
        contradictory -- one says 'don't fail the skill', the other
        says 'stop the skill on failure'. Normalize to
        on_failure="optional" when the convenience flag is set so the
        two fields can't disagree.

        Rejected the alternative of raising ValidationError because
        existing legacy skills may have ``optional=True`` alongside
        the default abort and we'd rather silently widen than break
        load. The normalization is conservative: optional=True wins
        because it's the more explicit operator intent."""
        if self.optional and self.on_failure == "abort":
            self.on_failure = "optional"
        return self


class AuthPrecondition(BaseModel):
    """WI-36: declarative auth requirement on a step.

    Steps that hit protected endpoints (state-changing requests against
    an authenticated API) require auth at replay time. The runner
    verifies the portal's auth signal BEFORE the step runs; on
    ``missing`` it pauses with a structured ``auth_missing`` failure
    (operator logs in and resumes) instead of looping or silently
    failing halfway through a save.

    Auto-relogin is OUT of scope for v1 (per the plan) -- this is a
    pure precondition check that surfaces the gap rather than trying
    to recover from it.

    The annotator emits this on steps detected as destructive (their
    folded network events include a PATCH/POST/PUT/DELETE) when the
    portal context declared an auth_signal. Steps without observed
    write traffic stay AuthPrecondition-free so read-only nav doesn't
    pause.
    """

    role_required: Optional[str] = None
    """Operator-declared role required to perform this step (e.g.
    'editor', 'admin'). Audit-only today -- the runner doesn't check
    a role claim; the auth_signal probe is binary (logged in or not).
    Reserved so a future WI can implement role-based gates."""

    session_namespace: Optional[str] = None
    """Identifier for which session must be active. Used by multi-tenant
    portals where the operator's tab may be authenticated to one tenant
    while the recording was captured under another. Audit-only today;
    the runner compares against PortalContext.session.notes when set."""

    refresh_strategy: Literal[
        "navigate", "reload", "sso_flow",
    ] = "navigate"
    """How the operator should recover when auth is missing.
      - ``navigate``: surface the login URL (PortalContext.session.login_url)
        as the resume hint. Operator opens it manually.
      - ``reload``: tell the operator to refresh the current tab (covers
        portals where session refresh is automatic on reload).
      - ``sso_flow``: SSO redirect chain expected; the operator's IdP
        will reauthenticate them and bounce back. Same UI as navigate;
        the label differs so the operator knows what to expect.
    Audit-only today. Auto-relogin is explicitly OUT of v1 scope."""


# ---------------------------------------------------------------------------
# WI-01: Provenance
#
# Replaces "templating by substring luck" (STRATEGIC item 2 in the
# audit). Param values become templated in URLs / selectors / IDs
# based on KNOWN sources (route param, request body, selected option,
# row key) rather than mechanical substring replace. The substring
# pass remains as a last-resort fallback for legacy skills, but new
# annotations should always have provenance.
# ---------------------------------------------------------------------------


class ParamProvenance(BaseModel):
    # Followup #1: audit-critical schema -- the matrix test passed for
    # weeks with ``ParamProvenance(source_step_index=2, ...)`` because
    # Pydantic dropped ``source_step_index`` silently while the real
    # field is ``source_step``. extra="forbid" surfaces the drift at
    # construction.
    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "operator_input",      # provided directly by the operator at replay
        "csv_row",             # parsed from a CSV row at intake
        "csv_column",          # parsed from a CSV column header
        "trace_recorded",      # captured value during teach
        "route_param",         # extracted from a URL path segment
        "request_param",       # extracted from a request query/body
        "selected_option",     # a <select>/listbox option that was chosen
        "file_metadata",       # filename / MIME / size
        "row_key",             # the key of a clicked table row
        "ancestor_attribute",  # an attribute on a parent element
    ]
    source_step: Optional[int] = None
    """Step index where the param value originated. Pairs with
    source_attribute to make the lineage explicit."""
    source_attribute: Optional[str] = None
    """Attribute / field name on the source. E.g. for source_step=4
    being a row click, source_attribute might be ``data-row-key``."""
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    """F-09: annotator's confidence in this provenance (0.0-1.0).
    Pydantic enforces the range; LLM-derived provenance carries lower
    confidence than deterministic, operator review can boost to 1.0."""


class StepProvenance(BaseModel):
    """Per-step audit trail of how the annotator constructed this step
    from the raw trace events. Lets a future operator (or auditor) see
    why a click became a fill_submit, or which raw events were
    collapsed into a set_selection."""

    raw_event_ids: list[str] = Field(default_factory=list)
    """IDs of the TraceEvents that contributed to this step. Populated
    once WI-02 lands; empty for legacy traces."""
    cluster_kind: Optional[str] = None
    """How the annotator classified this cluster: ``single_event``,
    ``fill_submit``, ``set_selection``, ``cascading_select``, ..."""
    detection_method: Optional[Literal["deterministic", "llm", "operator"]] = None


class ValueTransition(BaseModel):
    """WI-13: a field's value transition captured during teach.

    ``from_recorded`` is what the field held BEFORE the operator
    edited it (captured at focus / first input). ``to_recorded`` is
    the committed value. ``clear_intent`` is True when ``to_recorded``
    is empty AND ``from_recorded`` was non-empty AND the clear was
    followed by a commit signal (blur / submit / save click / field
    validation) -- meaning the operator deliberately erased the value.

    Today the legacy annotator filter dropped empty input changes,
    which silently lost workflows like "clear title and save." WI-13
    keeps the clear as a real semantic step, the runner fills an
    empty string and verifies the field reads empty after the action.
    """

    from_recorded: Optional[str] = None
    """Value before the operator started editing. None for legacy
    traces where the grabber didn't capture before-state."""
    to_recorded: str
    """Final committed value. Empty string is a valid clear-to-empty."""
    clear_intent: bool = False
    """True iff this transition is a deliberate clear (was non-empty,
    became empty, then committed). The runner uses this to decide
    whether to assert the field is empty after the action."""


class SemanticCluster(BaseModel):
    """WI-12: intermediate representation produced by the annotator's
    semantic clustering pipeline.

    The annotator runs in passes:
      (1) normalize raw events (WI-02 _assign_synthetic_ids)
      (2) build causality graph (WI-02 build_causality_graph)
      (3) detect widget clusters -- THIS model is the output
      (4) bind params + provenance (WI-11 _derive_provenance_templates)
      (5) produce semantic steps (cluster -> SkillStep)
      (6) emit expected_signals / assertions from observed effects

    Each cluster carries the raw events that compose it, the cluster
    kind (single_event, fill_submit, set_selection, ...), the primary
    target's fingerprint, optional folded effects, and a confidence
    score reflecting how certain the detection is. ``confidence=1.0``
    is reserved for deterministic detections; LLM-suggested clusters
    carry lower confidence and require operator review (future WI-31).

    The cluster is the BASIS for the SkillStep but isn't itself a
    step. The annotator converts each cluster into a SkillStep with
    StepProvenance.raw_event_ids = cluster.raw_event_ids. This split
    lets future WIs add cluster kinds (WI-15 fill_submit, WI-17
    select_option, WI-19 set_selection) without re-shaping the step
    schema each time -- the cluster carries the detection metadata,
    the step carries the runner contract.

    ``annotate_mode="linear"`` (legacy) skips this pipeline and emits
    one step per non-observed event. ``annotate_mode="semantic"``
    (default for new annotations) runs the full pipeline. Legacy traces
    can still be re-annotated in semantic mode; the LLM enrichment
    layer (WI-31) consumes the structured cluster list.
    """

    raw_event_ids: list[str] = Field(default_factory=list)
    """Event IDs (TraceEvent.event_id) that compose this cluster, in
    causal / sequence order. Always at least one; observed-event ids
    (network_request, dom_mutation) that contributed to the cluster
    are included so the audit log can reconstruct the recording."""
    cluster_kind: str
    """One of: ``single_event``, ``click_with_navigation`` (WI-08),
    ``fill_submit`` (WI-15), ``select_option`` (WI-17),
    ``select_autocomplete`` (WI-16), ``cascading_select`` (WI-18),
    ``set_selection`` (WI-19), ``modal_open`` / ``modal_close`` (WI-34),
    ``toggle_state`` (WI-33), ``date_select`` (WI-21),
    ``slider_set`` (WI-28), ``drag_drop`` (WI-30),
    ``rich_text_set`` (WI-39), ``download_click`` (WI-45),
    ``popup_open`` (WI-35), ``scroll_until`` (WI-38)."""
    primary_target_event_id: Optional[str] = None
    """The event the cluster ROOTS at. For a click-with-navigation
    cluster, the click event id; the navigate is a child. For a
    fill_submit cluster, the LAST input_change before the submit
    trigger. Used by the step-construction pass to pick the
    fingerprint + binding."""
    effects: Optional["StepEffect"] = None
    """Folded effects observed during this cluster's interaction
    window (navigation / network / dom / popup / etc). Lifted onto
    the SkillStep at step-construction time."""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    """0.0 - 1.0. 1.0 = deterministic detection. < 1.0 = LLM-suggested
    or heuristic; runner / UI may treat lower-confidence clusters as
    advisory until operator confirms."""
    alternatives_considered: list[str] = Field(default_factory=list)
    """Other cluster_kinds the detector evaluated and rejected. Empty
    for trivial clusters; populated when the detector picked between
    e.g. set_selection vs select_option for the same sequence. Useful
    for the LLM enrichment pass (WI-31) and operator review UI."""


class RepairPolicy(BaseModel):
    """WI-26: declarative policy for L3 (self-heal) acceptance.

    Today the L3 deterministic scorer maps best-match similarity to a
    confidence band via fixed thresholds (0.85 / 0.65 / 0.55), and the
    runner persists a "high" heal back onto the skill. The audit
    pointed out two problems:

      1. Score is meaningful only relative to candidate uniqueness.
         A 0.90-similarity match to one of TWO indistinguishable
         "Approve" buttons is still wrong.
      2. Score is not action-specific evidence. A click on a button
         that LOOKS like the recorded target but does nothing (e.g.
         disabled internally) gets persisted because the score was
         high enough, even when the action's intended postcondition
         never happened.

    RepairPolicy turns this into a structural contract:

      - required_features: which fingerprint attributes MUST match
        for the heal to be accepted (e.g. test_id_required = True
        means: heal candidate must carry a test_id; if score is high
        but candidate has none, reject).
      - uniqueness_scope: where the candidate must be unique
        (whole_page | within_section | within_row). A "high" score
        that's not unique under the declared scope -> reject.
      - allowed_drift_fields: fingerprint fields that are EXPECTED
        to change at replay (operator marks these as known-portal-
        drift; the scorer doesn't penalize their mismatch).
      - required_postcondition: an action-specific assertion kind
        (from StepAssertion) that MUST pass after the healed action
        for the heal to be PERSISTED. Score bands become tiebreakers,
        not gates.
      - medium_confidence_pauses: when True (default) a medium-score
        heal pauses for operator confirmation; legacy behavior was
        execute-without-verify-then-not-persist.

    The annotator emits RepairPolicy from observed fingerprint quality
    (test_id present => test_id_required; landmark stable =>
    within_section uniqueness) and the action's risk
    (requires_gate => stricter policy).
    """

    test_id_required: bool = False
    """When True, the heal candidate MUST carry a test_id. A high-
    similarity match without one is rejected. Default False (legacy
    skills); annotator sets True for steps whose original fingerprint
    had a test_id (the WI-26 contract: don't drift to a less-stable
    locator)."""

    role_match_required: bool = False
    """When True, the heal candidate MUST have the same ARIA role as
    the recorded fingerprint. Default False (back-compat). Annotator
    sets True for steps whose role was clearly the disambiguator
    (e.g. role=button on a click step)."""

    landmark_match_required: bool = False
    """When True, the heal candidate MUST live within the same landmark
    (dialog / panel / section) as the recorded fingerprint. Default
    False. Annotator sets True when landmark provenance was strong."""

    uniqueness_scope: Literal[
        "whole_page",
        "within_section",
        "within_row",
        "within_dialog",
    ] = "whole_page"
    """Where the healed candidate must be unique.
      - whole_page (default, legacy): only one such interactable on
        the page. Strictest for repeated controls.
      - within_section: only one inside the same landmark. Lets a
        page have multiple sections each with their own "Save."
      - within_row: only one inside the same table row. Strictest
        for row-scoped actions.
      - within_dialog: only one inside the active modal."""

    allowed_drift_fields: list[str] = Field(default_factory=list)
    """Fingerprint field names the annotator declares as expected to
    drift at replay (operator-curated). The scorer doesn't penalize
    their mismatch. Typical: ``element_id`` for ID-by-content-hash
    pages, ``css_path`` for shadow-DOM-heavy portals."""

    required_postcondition: Optional[str] = None
    """Action-specific assertion kind from StepAssertion that MUST
    pass post-action for the heal to be persisted. E.g.
    ``"selection_equals"`` for select_option, ``"url_matches_template"``
    for click-with-navigation. None means: legacy whole-page-signature
    check (the WI-27 replacement)."""

    medium_confidence_pauses: bool = True
    """WI-26: when True (default for destructive actions), a medium-
    score heal pauses for operator confirmation BEFORE the runner
    clicks. Pre-WI-26 medium executed silently then refused to
    persist on post-action drift; the operator never got to see /
    approve the substitution.

    Set False to preserve the legacy execute-but-don't-persist
    behavior (typically only for read-only / observational steps)."""


class AmbiguityPolicy(BaseModel):
    """WI-22: ambiguity detection configuration per step.

    Today only fires for templated test_id / element_id. WI-22 extends
    detection to every locator level (L1 exact, L2 semantic, alternates,
    L3 repaired) so the runner cannot silently click ``.first`` when a
    role + accessible name match yields multiple rows.

    The four fields here are the operator-visible knobs the annotator
    emits per step. Defaults are conservative: destructive actions
    require a unique candidate; non-destructive actions prompt the
    operator when multiple match.
    """

    locator_scope: Optional[str] = None
    """Optional CSS selector that NARROWS the candidate search before
    counting. E.g. ``[data-testid='row-{asset_id}']`` scopes the click
    target lookup to within ONE row. When None, the runner searches the
    whole page. Used by the grabber's ancestor-chain capture to identify
    the closest dialog / row / card / section the click target lived
    under -- the same scope at replay produces the same uniqueness
    guarantees."""

    expected_candidate_count: int = 1
    """How many visible elements the locator should resolve to. The
    default ``1`` matches the recording's targeted-at-one-element
    semantics. >1 is valid for batch operations (e.g. 'select all rows
    matching X'). When the runner counts more than this, ambiguity
    detection fires.

    Setting to ``0`` opts out entirely -- legacy escape hatch for cases
    where the recording is known to be a multi-match (e.g. a generic
    'click visible toast' where the runner takes any toast). Not
    recommended for new annotations."""

    ambiguity_policy: Literal[
        "fail_if_multiple",
        "pick_first",
        "prompt",
    ] = "fail_if_multiple"
    """How to handle a multiple-candidate situation.
      - ``fail_if_multiple`` (default for destructive actions): emit
        ``ambiguous_target`` and pause the runner. The orchestrator
        surfaces the row picker; operator picks one.
      - ``pick_first``: legacy behavior. Runner clicks ``.first`` and
        carries on. ONLY safe when the recording explicitly targeted the
        first match (rare; usually a recording bug).
      - ``prompt``: same as ``fail_if_multiple`` but treated as a
        non-fatal pause -- the runner surfaces the picker but operator
        can also choose 'continue with first'."""

    candidate_context_fields: list[str] = Field(
        default_factory=lambda: ["test_id", "text", "id", "role"]
    )
    """Which fingerprint fields the runner includes in the candidates
    summary surfaced as ``ambiguous_target.error_details.candidates``.
    The UI's row-picker modal renders these so the operator can tell
    candidate rows apart. Default covers the common identifying fields;
    operator can extend per portal (e.g. add ``aria-label`` for
    accessibility-driven pickers)."""


class DisambiguationHint(BaseModel):
    """Features captured when an operator resolved an ambiguous_target.

    Persisted onto the step that produced the ambiguity. Next replay,
    the runner scores candidates against the hint BEFORE pausing for
    operator input; if there's an unambiguous winner the click goes
    straight through.

    Includes negative_candidates -- the operator's rejected picks --
    because "the operator chose B over A and C" is stronger evidence
    than "B looked like this."
    """

    chosen_text: Optional[str] = None
    chosen_test_id: Optional[str] = None
    chosen_neighbors: list[str] = Field(default_factory=list)
    """Visible text from sibling rows/cells, helps tie-break by context."""
    chosen_section: Optional[str] = None
    """Landmark / section name the chosen element lived in."""
    negative_candidates: list[dict[str, Any]] = Field(default_factory=list)
    """The candidate dicts the operator did NOT pick. Each carries the
    same shape as the ambiguous_target candidates payload."""


class FillSubmitSpec(BaseModel):
    """WI-15: spec for a text-input burst + submit, collapsed to ONE step.

    The recording captured the operator typing into a single input
    (potentially many input_change events as the value evolved) followed
    by a commit signal (Enter key, click on a submit button, or form
    submit). The annotator collapses this whole burst into one
    ``fill_submit`` step parameterized by the final value, with this
    spec describing HOW to commit.

    At replay the runner:
      1. Resolves the field locator from the step.fingerprint.
      2. Fills the resolved value ONCE.
      3. Triggers the declared submit_trigger exactly ONCE.
      4. If expected_result_signal is declared, waits for it.

    Why this exists (acceptance check, from the plan):
      Catalog search 'A-9003' should emit ONE fill_submit step with the
      final value, not 4 fills + Enter + a separate submit. Replaying
      4 fills causes intermediate-value race conditions (autocomplete
      results from 'A-9' that never get rendered against the final
      query 'A-9003'); replaying Enter + submit click can double-fire
      the form when the click already implies submit.
    """

    submit_trigger: Literal["enter", "button", "form_submit"]
    """How the operator committed the typing burst.
      - ``enter``: a key=Enter event on the same field; runner presses
        Enter on the field after filling.
      - ``button``: the operator clicked a submit button; spec carries
        ``submit_button_fp`` and the runner clicks it after filling.
      - ``form_submit``: a form's submit fired (without an explicit
        click). The runner falls back to pressing Enter on the field
        because Playwright doesn't expose ``form.submit()`` cleanly;
        the form's onsubmit handler still runs."""

    value_param: Optional[str] = None
    """Name of the skill param holding the final value. None means the
    annotator couldn't bind a param (operator typed a literal that
    doesn't appear in skill.params) -- the runner falls back to
    ``step.value`` in that case."""

    submit_button_fp: Optional[ElementFingerprint] = None
    """When submit_trigger == ``button``: the submit button's
    fingerprint. None for ``enter`` and ``form_submit``."""

    expected_result_signal: Optional[str] = None
    """Optional URL substring of the network request the submit is
    expected to trigger (search result endpoint, etc.). When set, the
    runner waits for a matching response after submit. None falls back
    to the generic page-settle heuristic."""


class AutocompleteSpec(BaseModel):
    """WI-16: spec for a search query + result selection, collapsed to
    ONE step.

    The recording captured the operator typing a query into an
    autocomplete input, the page firing a backend search, results
    appearing in a container, and the operator clicking one of the
    results. The annotator collapses this whole interaction into a
    single ``select_autocomplete`` step.

    The recording separates two params -- ``query`` (the typed search
    text) and ``selected_item`` (the result the operator clicked) --
    because an operator may want a DIFFERENT result at replay than the
    one they originally clicked, while keeping the query identical.
    E.g. recorded ``search 'A-90' -> pick A-9003`` should be replayable
    as ``search 'A-90' -> pick A-9002`` without re-recording.

    Acceptance check (from the plan):
      Search 'A-90' + pick A-9003 at recording; replay can pick A-9002
      with a different selected_item without re-recording.
    """

    query_param: str
    """Name of the param holding the search query text. Resolved to a
    string at replay and filled into the query input."""

    selected_item_param: str
    """Name of the param holding the result identity to pick. Distinct
    from query_param so the operator can vary the pick independently."""

    query_input_fp: Optional[ElementFingerprint] = None
    """Fingerprint of the search input. When None the runner uses the
    step.fingerprint (which the annotator sets to the input)."""

    result_container_fp: Optional[ElementFingerprint] = None
    """Fingerprint of the result container element (listbox, dropdown,
    panel) that appears AFTER the query fires. The runner waits for
    this container to become visible before looking for the chosen
    option. None falls back to the generic page-settle wait."""

    option_identity_template: Optional[ElementFingerprint] = None
    """Fingerprint TEMPLATE for the option to click; carries a ``{item}``
    placeholder in test_id / element_id / etc. The runner materializes
    the template with the resolved ``selected_item_param`` value, then
    clicks. Provenance-derived (WI-11), so 'btn-open-A-9003' templates
    to 'btn-open-{selected_item}' from the row_key source, never from
    substring luck."""

    network_expectation: Optional["NetworkExpectation"] = None
    """The backend search request the typed query triggers. When set,
    the runner waits for this request to complete BEFORE clicking the
    result -- otherwise it would race against stale options."""


class SelectOptionSpec(BaseModel):
    """WI-17: spec for native <select> single-select with declared
    aliases and structural fail-fast.

    Replaces the legacy ``_select_option_with_fuzzy_fallback`` which
    auto-fuzzy-matched the recorded value against current options when
    the exact value was missing. The fuzzy threshold (0.7 cosine) could
    confuse ``US`` with ``UAE`` or ``UK`` -- a wrong locale / status
    selection that the operator never authorized.

    Match priority (NO fuzzy unless aliases declared):
      1. Exact ``value`` match against the option's value.
      2. Exact ``label`` match against the option's visible text.
      3. Declared ``aliases``: a recorded value of ``US`` can be told
         to match labels ``USA`` / ``United States``.
      4. ``current_options`` mode: ignore the recorded value, pick
         from current options by index/criteria (for state-dependent
         dropdowns like asset status transitions).
      5. Fail structurally with ``option_not_available`` listing what
         IS available.

    Acceptance check (from the plan):
      Recorded ``US`` never auto-selects ``UAE``; runner fails
      structurally without an alias.
    """

    recorded_value: str
    """The option's value attribute at record time."""

    recorded_label: Optional[str] = None
    """The option's visible text at record time. Used by ``label`` and
    ``alias`` match modes."""

    options_snapshot: Optional[list["OptionSnapshot"]] = None
    """Full option list (value + label + selected + disabled) captured
    at record time. Used by the runner to suggest available options
    in the structural failure message and to detect when the option
    set has stabilized after a cascading parent change."""

    match_mode: Literal["value", "label", "alias", "current_options"] = "value"
    """How to match the recorded value against the current options.
      - ``value`` (default): exact option.value match.
      - ``label``: exact option text match.
      - ``alias``: try value, then label, then aliases.
      - ``current_options``: ignore recorded, select from current
        options by recorded_index (for dynamic option sets)."""

    aliases: dict[str, list[str]] = Field(default_factory=dict)
    """Operator-declared aliases. Key is the recorded value or label;
    value is a list of acceptable alternatives. E.g.
    ``{"US": ["USA", "United States"]}``. Aliases are tried only when
    match_mode == ``alias``. Empty dict means no aliases declared."""

    recorded_index: Optional[int] = None
    """For ``current_options`` mode: which index in the current option
    list to pick. Useful when the option order is stable but values
    change (e.g. status transitions where you always want the SECOND
    transition)."""

    option_match_policy: Literal[
        "exact_only",
        "alias",
        "current_options",
        "legacy_fuzzy",
    ] = "exact_only"
    """WI-25: the structural policy controlling auto-fuzzy fallback.

    - ``exact_only`` (default for new skills): value/label exact match;
      no aliases consulted; no fuzzy. If the recorded option isn't in
      the current set, the runner fails with ``option_not_available``
      listing the available options.
    - ``alias``: same as exact_only PLUS the aliases declared via
      ``aliases`` (and the SkillParam's ``enum_aliases``) are tried.
      No fuzzy. Operator-curated only.
    - ``current_options``: ignore recorded value; use ``recorded_index``
      to pick from current options. For state-dependent dropdowns where
      the value set drifts but the order is stable.
    - ``legacy_fuzzy``: route through
      ``_select_option_with_fuzzy_fallback``. Preserved ONLY for
      back-compat with skills recorded before WI-25 that lacked
      options_snapshot; new annotations MUST NOT emit this.

    The audit's specific failure mode -- recorded ``US`` auto-matches
    ``UAE`` because difflib similarity 0.71 cleared the 0.7 threshold
    -- is structurally impossible under exact_only / alias /
    current_options. Only legacy_fuzzy retains the dangerous path."""


class DependencyChain(BaseModel):
    """WI-18: declares a chain of parent->child select dependencies.

    Captures: 'changing the region select causes the markets select to
    refresh its options via a request to /api/markets?region=X'. The
    annotator attaches this chain to the PARENT select's step so the
    runner can wait for the child options to refresh after the parent
    change before the child's own select_option step runs.

    Without this, a cascading-select replay races: it changes the
    parent, then immediately tries to pick the recorded child value,
    but the child's option list hasn't refreshed yet -- so the runner
    either picks a stale option (correct value, wrong meaning) or fails
    because the recorded option no longer exists.

    Acceptance check (from the plan):
      Change region=APAC, replay picks Market from APAC list; recorded
      California (USA) under India fails cleanly with available Indian
      markets -- NO fuzzy fallback on cascading deps.
    """

    parent_param: str
    """Name of the parent param (the one whose change drives the
    dependent). E.g. ``region``."""

    child_param: str
    """Name of the dependent param. E.g. ``market``."""

    option_source_request: Optional["NetworkExpectation"] = None
    """The backend request the parent change is expected to trigger to
    refresh the child's options. Runner waits for this to complete
    before the child step runs."""

    child_options_signature_after: Optional[str] = None
    """Optional CSS selector pointing at the child select. Runner reads
    the option count / values from this selector after the parent
    change and the request completes, verifying that the option set
    actually mutated (and not just 'request completed but options
    didn't change' which would also be a structural error)."""


class DatePickerSpec(BaseModel):
    """WI-21: spec for native or custom date pickers.

    Two kinds:
      - ``native``: ``<input type="date">`` (or ``datetime-local``,
        ``time``, ``month``, ``week``). Runner sets the ISO-formatted
        value directly and dispatches ``input`` + ``change`` events.
      - ``custom``: a calendar grid widget (React DatePicker,
        Material-UI, etc.) where the operator clicked a specific day
        cell. The annotator detected the cluster from the calendar
        role + cell clicks. At replay the runner DOES NOT replay the
        recorded click path (the calendar may be on a different month
        / year for a different target date); instead it navigates by
        SEMANTIC date target (next/prev month buttons until the target
        month is showing, then click the cell matching the target day).

    Both kinds use ``value_param`` to bind a date param. The codec on
    the SkillParam (``iso_date`` from WI-05) normalizes the operator's
    input (any locale) to ISO ``YYYY-MM-DD`` before the runner consumes
    it.

    Acceptance check (from the plan):
      Recorded date 2026-05-20 replays a different date without
      needing the same calendar nav clicks.
    """

    kind: Literal["native", "custom"]
    """``native`` for HTML date inputs (instant value set); ``custom``
    for calendar-widget pickers (semantic navigation)."""

    value_param: str
    """Name of the date param (typed ``date`` with ``iso_date`` codec).
    Runner reads the resolved ISO date from ``self.params[value_param]``."""

    display_format: Optional[str] = None
    """For ``custom`` pickers that show dates in a specific format
    (e.g. ``MM/DD/YYYY``). The runner uses this to read the currently-
    shown month/year from the widget's header. None falls back to
    standard ISO parsing."""

    timezone: Optional[str] = None
    """IANA timezone the operator's portal renders dates in. None means
    use the recorded portal's timezone_hint (or UTC if absent)."""

    calendar_grid_fp: Optional[ElementFingerprint] = None
    """For ``custom``: fingerprint of the calendar grid container
    (role=grid). Runner scopes its cell-click search inside this."""

    prev_month_fp: Optional[ElementFingerprint] = None
    next_month_fp: Optional[ElementFingerprint] = None
    """For ``custom``: navigation buttons to move the visible month
    backward / forward by one month."""

    month_year_label_fp: Optional[ElementFingerprint] = None
    """For ``custom``: the label that shows the currently-displayed
    month + year (e.g. 'May 2026'). Runner reads this to know whether
    to advance or retreat before clicking a day cell."""

    day_cell_template_fp: Optional[ElementFingerprint] = None
    """For ``custom``: cell click target template with a ``{day}``
    placeholder. Materialized with the target day-of-month (1-31)."""

    selected_day_identity: Optional[dict[str, Any]] = None
    """Optional record-time snapshot of which day cell was clicked
    (month/year/day). Audit-only; the runner picks by target_date, not
    by recorded click."""


class ScrollUntilSpec(BaseModel):
    """WI-37 + WI-38: scroll a declared scroller until a target row /
    element is visible inside its viewport.

    Replaces the legacy 'scroll for N times' heuristic with: scroll one
    viewport, re-probe target via the declared
    target_visible_in_scroller DomExpectation, repeat until visible or
    ``max_scrolls`` exhausted. The scroller's identity comes from
    ``scroller_fp`` (NOT a hardcoded window / body) so virtualized
    tables, infinite-scroll lists, and custom scroll containers all
    work the same way.

    Acceptance (from the plan):
      A recording that scrolled past 50 rows to click row 75 can
      replay against a list that has row 75 at any current scroll
      position. The runner stops scrolling as soon as the target
      becomes visible -- it doesn't blindly replay the recorded
      scroll count.
    """

    scroller_fp: ElementFingerprint
    """Fingerprint of the SCROLLER -- the element that received the
    operator's wheel/touch events. Resolved at replay through the
    standard L1/L2/L3 cascade. The runner dispatches wheel events
    against this element (NOT the window) so virtualized lists hosted
    inside a div remain scrollable."""

    target_identity: dict[str, Any] = Field(default_factory=dict)
    """Identifying attributes of the TARGET the operator was scrolling
    to find. Shape: {test_id?: str, element_id?: str, row_key?: str,
    text?: str}. The runner uses this to materialize a target selector
    inside the scroller via the same identity precedence as L1 lookup.
    Empty dict means the scroll is purely time-based (no termination
    target) -- annotator emits an empty dict only when no visibility
    change was observed."""

    max_scrolls: int = 50
    """Hard cap on scroll iterations. 50 covers the WI-37 acceptance
    check (row 75 at page size 20 -> ~4 scrolls; 50 is generous for
    virtualized lists that paginate every 10 rows). Tune upward for
    extremely deep tables."""

    page_size_hint: Optional[int] = None
    """Approximate number of rows rendered per viewport. Audit-only
    hint -- the runner uses it to size the wheel delta (one viewport
    = scroll by viewport height). When None the runner reads
    scroller.clientHeight at replay."""

    network_signal: Optional[NetworkExpectation] = None
    """Optional network expectation that gates 'more rows have
    loaded.' For virtual / infinite-scroll lists backed by a network
    pagination endpoint -- the runner waits for this between scrolls
    so the next scroll has rows to render. None for purely
    client-rendered lists where scrolling alone reveals new rows."""

    scroll_direction: Literal["down", "up"] = "down"
    """Which way to scroll. ``down`` is the common case (forward
    pagination); ``up`` for reverse / 'load earlier' patterns. Audit-
    only; the runner dispatches wheel events with positive deltaY for
    down and negative for up."""


class ToggleStateSpec(BaseModel):
    """WI-33: spec for accordion/expand-collapse toggle as DESIRED
    state.

    Today an operator clicks the accordion header; the runner clicks
    blindly even if the panel is already in the desired state. That
    breaks replays where the page starts in a different state (e.g.
    the operator recorded 'click to expand,' but at replay the panel
    is already expanded -- a click would collapse it).

    This spec moves the runner from 'click N times' to 'reach this
    state, clicking only when current != target.' The grabber's WI-14
    target_state_after captures the desired state at record time; the
    annotator stamps it into ToggleStateSpec.target_state. At replay,
    the runner reads the current state from the recorded state
    attribute and skips the click when state already matches.

    Acceptance check (from the plan):
      Replay leaves the panel EXPANDED regardless of starting state.
      - Already collapsed at replay -> click to expand.
      - Already expanded at replay -> no-op.
    """

    target_state: bool
    """The desired post-action state. True = expanded / pressed /
    checked / open; False = collapsed / unpressed / unchecked /
    closed. Read from the grabber's target_state_after at annotate
    time."""

    state_attribute: Literal[
        "aria-expanded",
        "aria-pressed",
        "aria-checked",
        "data-state",
    ] = "aria-expanded"
    """Which attribute the runner reads to determine current state.
    Most accordions use aria-expanded; toggle buttons may use
    aria-pressed; checkboxes use aria-checked; some design systems
    use data-state with values like 'open'/'closed'."""

    controlled_panel_selector: Optional[str] = None
    """CSS selector for the panel the toggle controls (typically
    referenced by aria-controls on the trigger). When set, the runner
    can verify the panel's visibility matches target_state post-
    action. None falls back to the state attribute as the sole
    verification."""


class DragDropSpec(BaseModel):
    """WI-30: spec for a drag-and-drop interaction.

    The recording captured the operator's dragstart on a source
    element, optional dragover events on the target zone, and the
    final drop. The grabber emits one drag_drop trace event PER
    sequence (or three event kinds the annotator collapses).

    The annotator collapses the dragstart -> drop sequence into ONE
    drag_drop step. At replay the runner prefers Playwright's
    locator.drag_to API (high-level, fires the right events) when both
    source and target are reliably locatable; the JS-DataTransfer
    fallback fires only when the spec declares it safe.

    Acceptance check (from the plan):
      Dragging an item into a drop zone records, replays, and asserts
      the target contains the dragged item.
    """

    source_fp: ElementFingerprint
    """Fingerprint of the source element (the item being dragged).
    Resolved at replay through the standard L1/L2/L3 cascade."""

    target_fp: ElementFingerprint
    """Fingerprint of the target drop zone (the container that
    receives the dropped item)."""

    data_payload_summary: Optional[dict[str, Any]] = None
    """Compact summary of the DataTransfer payload at record time --
    types[] + first 200 chars of the text/plain value if present. NOT
    used for replay; preserved for audit so an operator can see what
    payload was carried. The runner uses the source's identifying
    attribute (test_id) as the payload at replay; portals that depend
    on a specific DataTransfer value need a portal-specific adapter."""

    drop_effect: Literal["move", "copy", "link", "none"] = "move"
    """DataTransfer.dropEffect at record time. ``move`` is the default
    for sortable lists; ``copy`` for clipboard-style drag; ``link`` is
    rare. Runner sets dataTransfer.effectAllowed when using the JS
    fallback."""

    coordinates_policy: Literal[
        "center", "absolute", "relative"
    ] = "center"
    """Where on the target the drop fires:
      - ``center`` (default): aim at the target's center; works for
        most droppable containers.
      - ``absolute``: use recorded (clientX, clientY). Fragile when
        the page resizes between recording and replay.
      - ``relative``: offset from the target's top-left by the
        recorded delta. Mid-confidence; only safe when target
        dimensions are stable."""

    use_high_level_api: bool = True
    """When True (default) the runner uses Playwright's
    ``source_locator.drag_to(target_locator)`` -- the high-level API
    that fires dragstart/drag/dragover/drop with browser-typical
    timing. When False the runner falls back to manual pointer.move +
    DataTransfer dispatch, used only for portals that observed a
    specific DataTransfer payload requirement at recording. The
    annotator emits True for vanilla HTML5 drag/drop and False when
    the recording shows DataTransfer.setData with non-test-id
    payloads."""


class FileMetadata(BaseModel):
    """WI-29: one file's metadata captured at record time.

    The browser never exposes the absolute path for security reasons;
    only the original name, size in bytes, and MIME type are accessible.
    Plus the extension extracted from the name for cheap accept-attr
    matching at replay. The annotator stamps this on every recorded
    file so the runner can validate the replay-time replacement file
    against the recorded constraints BEFORE the page mutates."""

    name: str
    """Original filename at record time. Not used as a path -- replay
    requires an operator-supplied path."""
    size: Optional[int] = None
    """File size in bytes (browser-reported)."""
    mime: Optional[str] = None
    """MIME type the browser inferred from the file. May be empty for
    unknown types -- the runner falls back to extension matching."""
    ext: Optional[str] = None
    """Lowercased extension including the leading dot, e.g. ``.png``.
    Extracted from ``name`` at record time."""


class FileSpec(BaseModel):
    """WI-29: spec for a file upload step.

    Carries the recorded constraints (original name, MIME, accept attr,
    multiple flag) so the runner can validate the operator-supplied
    REPLACEMENT path against them BEFORE the page mutation.

    The param itself remains ``file_path`` (single) or ``file_path_list``
    (when ``multiple_flag`` is True); this spec is the contract the
    runner checks the param value against.

    Acceptance check (from the plan):
      Replay with a different file path succeeds when constraints
      match; fails BEFORE the page mutation when file is missing or
      MIME / extension doesn't match the recorded accept attribute.
    """

    original_name: Optional[str] = None
    """Recorded filename at record time. Audit-only -- replay passes
    a NEW path."""
    extension: Optional[str] = None
    """Lowercased extension (e.g. ``.png``)."""
    mime_hint: Optional[str] = None
    """Recorded MIME type."""
    size: Optional[int] = None
    """Recorded file size in bytes."""
    accept_attribute: Optional[str] = None
    """The ``accept`` attribute on the file input at record time. Used
    by the runner to validate that the replay-time path's extension /
    MIME matches. Comma-separated list of extensions and MIME globs
    (e.g. ``image/*,.pdf``). None means the input had no constraint."""
    multiple_flag: bool = False
    """Whether the input was ``multiple``. When True the replay param
    is treated as ``file_path_list``; when False it's ``file_path``."""
    recorded_files: list[FileMetadata] = Field(default_factory=list)
    """For multiple-file uploads: the per-file metadata snapshot. Audit-
    only -- replay still supplies its own paths."""


class ShortcutSpec(BaseModel):
    """WI-41: spec for a global keyboard shortcut (Ctrl+S, Cmd+K,
    Escape, etc.).

    Distinguished from a text-input ``key`` event: a shortcut is a
    chord (modifiers + key) fired OUTSIDE a text input, OR is one of
    the universal shortcuts (Escape, Tab) that's meaningful even
    inside an input. The grabber emits ``kind='key'`` with
    raw_event_kind='shortcut' to mark these; the annotator collapses
    them into a single ``shortcut`` step.

    At replay the runner:
      1. Optionally focuses a specific element when scope=focused_element.
      2. Issues page.keyboard.press(combo) -- single call; Playwright
         dispatches the right keydown/keyup pairs.
      3. Waits for expected_effect (URL change / DOM appears / network)
         when declared. Without an expected_effect the runner returns
         immediately after the press.

    Acceptance check (from the WI brief):
      Ctrl+S as a save shortcut records once + replays without typing
      into the focused field.
    """

    modifiers: list[Literal["Control", "Meta", "Shift", "Alt"]] = Field(
        default_factory=list
    )
    """The chord's modifier keys, normalized to Playwright key names
    (Control / Meta / Shift / Alt). Order doesn't matter; the runner
    joins them with '+' in the press call. Empty list = bare key
    (e.g. Escape, Tab)."""

    key: str
    """The non-modifier key in Playwright's keyboard-event-key form.
    e.g. 's' (Ctrl+S), 'k' (Cmd+K), 'Escape', 'Tab', '/' (search
    shortcut). NOT the KeyboardEvent.code -- Playwright maps the
    'key' form to the right physical key on the active layout."""

    scope: Literal["global", "focused_element", "command_palette"] = "global"
    """Where the shortcut applies.
      - ``global``: fired against document.activeElement at replay --
        Playwright's page.keyboard.press without an element focus.
      - ``focused_element``: shortcut requires a specific element to
        be focused first (e.g. Ctrl+Enter inside a comment box).
        ``focus_target_fp`` carries the element to focus before
        pressing.
      - ``command_palette``: invokes a command palette overlay
        (Ctrl+K / Cmd+P). Same dispatch as ``global``; the scope is
        a hint for documentation / future palette-aware verification.
    """

    focus_target_fp: Optional[ElementFingerprint] = None
    """For scope=focused_element: the element to focus before pressing.
    None for ``global`` / ``command_palette``."""

    expected_effect: Optional[Literal[
        "navigation", "modal_open", "save_completes",
        "command_palette_open", "form_submit", "noop",
    ]] = None
    """Declarative hint about what the shortcut SHOULD do. Drives the
    runner's post-press wait:
      - ``navigation``: wait for URL change.
      - ``modal_open``: wait for a role=dialog to appear.
      - ``save_completes``: wait for the step's expected_signals
        (declared separately on the step) -- the shortcut step still
        carries its own expected_signals; this enum just documents
        intent.
      - ``command_palette_open``: wait for a known palette selector
        (declared via command_palette_selector when set).
      - ``form_submit``: like save_completes; expects a POST/PATCH.
      - ``noop``: documentation-only; no wait.
    """

    command_palette_selector: Optional[str] = None
    """For expected_effect=command_palette_open: CSS selector for the
    palette container expected to appear. None falls back to the
    step's expected_signals."""


class RichTextSpec(BaseModel):
    """WI-39: spec for setting a contenteditable / rich-text editor's
    content as ONE semantic step.

    Plain ``<input>`` / ``<textarea>`` go through fill_submit / change.
    Contenteditable roots -- and their embedded editors (TinyMCE, Quill,
    Lexical, ProseMirror, Slate) -- need a different replay path because
    Playwright's ``locator.fill`` does not work on a non-form-control
    contenteditable (it returns an empty string).

    The annotator collapses the operator's typing burst inside a
    contenteditable into ONE ``rich_text_set`` step whose primary target
    is the editor root. At replay the runner:

      1. Resolves the editor root via the step's fingerprint.
      2. Focuses the root + clears existing content per ``embed_policy``.
      3. Sets the content via the declared ``paste_strategy`` so the
         framework's listeners (input event, beforeinput, paste) fire
         the right way for that editor.
      4. Verifies the editor's text/HTML matches the resolved param.

    Acceptance check (from the WI brief):
      Typing 'Hello **bold**' into a contenteditable replays the same
      content at replay time -- including any sanitization the framework
      performs on innerHTML insertion.
    """

    value_param: str
    """Name of the skill param holding the target content. Resolved at
    replay through the SkillParam's codec (typically ``raw`` for
    plain/markdown, with the editor's own sanitizer doing the HTML
    parsing)."""

    format: Literal["html", "markdown", "plain"] = "plain"
    """How the param value is interpreted.
      - ``plain``: textContent set, no HTML parsing. Safe default for
        comments / notes where formatting doesn't matter.
      - ``html``: innerHTML set, then the editor framework's mutation
        observer normalizes (Quill / Lexical / ProseMirror rewrite HTML
        to their internal model representation on input).
      - ``markdown``: pass-through to the editor's ``setMarkdown`` API
        when the framework exposes one (currently Lexical + ProseMirror's
        markdown plugin). Otherwise the runner falls back to plain and
        emits a warning so the operator knows the formatting was lost.
    """

    embed_policy: Literal["preserve", "strip"] = "preserve"
    """How to handle EMBEDDED non-text nodes (images, mentions, embeds)
    when clearing the editor before insertion.
      - ``preserve``: leave existing embeds in place; the new content is
        appended or replaces the text portion only.
      - ``strip``: clear the whole editor (innerHTML='') before inserting
        the new content. Required for ``html`` / ``markdown`` formats
        where the param value carries the FULL document state, not a
        delta. Default for ``html`` / ``markdown``; ``preserve`` for
        ``plain`` so a comment edit doesn't accidentally drop an image.
    """

    paste_strategy: Literal[
        "execCommand", "clipboard", "input_event"
    ] = "input_event"
    """How the runner injects the content into the editor.

      - ``execCommand``: ``document.execCommand('insertHTML', ...)``.
        Works on legacy editors that listen for execCommand events.
        Deprecated in modern browsers but still fires the input handler
        for TinyMCE / older Quill.
      - ``clipboard``: simulate a paste via the ``ClipboardEvent`` and
        ``DataTransfer`` API. Most modern editors (Lexical, ProseMirror)
        handle paste through this path and call their own sanitizer.
      - ``input_event``: set innerHTML / textContent directly, then
        dispatch a synthetic ``input`` event so React/Vue controlled
        editors see the change. DEFAULT because it's the most portable
        across editor frameworks; the editor's own MutationObserver
        re-normalizes after the synthetic event fires.

    The annotator picks ``input_event`` by default. Operators can
    override per-editor in the portal context when the default doesn't
    work for a specific portal's editor framework."""

    editor_root_fp: Optional[ElementFingerprint] = None
    """Fingerprint of the contenteditable root (the element carrying
    ``contenteditable=true`` directly OR the ancestor whose contenteditable
    cascade applies). When None the runner uses step.fingerprint -- which
    the annotator sets to the same root. Kept separate so future WIs that
    target a specific block inside an editor (e.g. only set a paragraph)
    can scope the fingerprint to the block while the editor_root_fp
    points at the framework's mount node."""

    framework_hint: Optional[Literal[
        "tinymce", "quill", "lexical", "prosemirror", "slate", "draft",
        "unknown",
    ]] = None
    """Detected editor framework. The grabber inspects window-scoped
    instances (window.tinymce, window.Quill, document.querySelectorAll
    for editor classes) at record time and stamps the hint here. The
    runner uses it to pick a framework-specific API path BEFORE falling
    back to ``paste_strategy``. ``unknown`` means raw contenteditable
    (no detected framework) -- safe default."""

    recorded_html: Optional[str] = None
    """The editor's innerHTML at the END of the recorded typing burst.
    Audit-only; the runner uses the param value, not this snapshot. Kept
    so the operator can review what was originally typed without
    replaying."""

    recorded_text: Optional[str] = None
    """The editor's textContent at the end of the burst. Audit-only;
    same role as recorded_html for the plain-text path."""


class SliderSpec(BaseModel):
    """WI-28: spec for a range slider (``<input type=range>``) final
    value + event dispatch.

    The recording captures a drag burst (multiple ``input`` events fired
    by the browser as the slider thumb moves, then a final ``change``
    event on release). The annotator collapses this burst into ONE
    ``slider_set`` step parameterized by the FINAL committed value.

    At replay the runner:
      1. Resolves the slider locator from step.fingerprint.
      2. Sets the value via ``element.value = X`` + dispatches input +
         change events (mirrors what the browser does on a real drag
         commit). ``locator.fill`` works for ``type=range`` in modern
         Playwright but the explicit JS path is portable across
         older Playwright versions AND ensures the synthetic event
         sequence matches what React/Vue controlled sliders listen for.
      3. Verifies the slider's final value matches the target.

    Acceptance check (from the plan):
      Moving slider during teach emits one ``slider_set`` step; replay
      sets a DIFFERENT value and verifies it.
    """

    value_param: str
    """Name of the param holding the target slider value. Resolved at
    replay through the SkillParam's codec (typically ``raw`` since the
    value is already a number string)."""

    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    """Slider's HTML constraints captured at record time. The annotator
    surfaces these as ParamConstraints (min/max) on the SkillParam so
    the runner rejects out-of-range values BEFORE setting them on the
    page. ``step`` is informational -- the runner doesn't enforce
    quantization (the browser does)."""

    orientation: Literal["horizontal", "vertical"] = "horizontal"
    """Slider orientation. ``vertical`` is rare but valid (aria-
    orientation=vertical or CSS-rotated thumb). Audit-only today; the
    JS-dispatch path doesn't differ by orientation."""

    event_mode: Literal["input", "change", "both"] = "both"
    """Which DOM events the page listens for. Most React controlled
    sliders bind onChange to ``input`` (live updates); native form
    handlers expect ``change``. ``both`` (default) dispatches both so
    we cover both shapes -- the runner is conservative."""

    final_value: Optional[str] = None
    """Record-time committed value (the value at the LAST input/change
    in the drag burst). Audit-only -- the runner uses
    ``self.params[value_param]`` for the target. Kept so the operator
    can see what was originally recorded."""


class PageContext(BaseModel):
    """WI-46: cross-tab / multi-window page binding for a step.

    Builds on WI-35's popup capture + page registry. When a workflow
    continues inside a popup (or any non-primary page), the affected
    SkillStep declares a PageContext pointing at the popup's
    ``page_binding_key`` (from PopupEffect). At replay the runner
    routes all locator resolution + actions for the step to
    ``BrowserSession.popup_pages[page_binding_key]`` instead of the
    main session.page.

    ``opens_via`` is informational provenance about how the page got
    bound (which prior step's effects.popup.page_binding_key matches).

    ``expected_close`` declares how the popup should leave the
    operator's session:
      - ``auto``: the popup closes naturally as a consequence of the
        action; the runner verifies the popup is gone on the NEXT
        main-page step (covers print previews that self-dismiss).
      - ``user``: the operator closes the popup mid-workflow; the
        runner doesn't verify, just observes.
      - ``action``: this step's action causes the popup to close; the
        runner verifies the popup is gone immediately after the action.
    """

    page_binding_key: str
    """The key the runner looks up in BrowserSession.popup_pages. Must
    match a key declared on a prior step's effects.popup."""

    opens_via: Optional[str] = None
    """Informational: the cluster_kind or step.semantic_label that
    opened this page (e.g. ``popup_open`` step at index 4). Audit-only
    -- the runner doesn't use it for routing."""

    expected_close: Literal["auto", "user", "action"] = "auto"
    """When the popup is expected to close relative to this step. See
    class docstring for semantics."""


class CanvasGestureSpec(BaseModel):
    """WI-49: spec for a ``canvas_gesture`` step.

    Canvas / SVG / media-timeline controls cannot be replayed through
    generic DOM locators -- the operator's recorded click coordinates
    don't survive layout changes, and the canvas pixel content is
    invisible to ARIA. The fix is to dispatch through a per-portal
    adapter that takes a STRUCTURED intent (e.g. ``{kind: 'draw_box',
    coords: [x, y, w, h]}``) and produces the Playwright pointer events
    that realize that intent on the specific canvas / SVG library the
    portal uses.

    The runner looks up the adapter by ``adapter_name`` in the
    ``pilot.adapters`` canvas registry (see
    ``pilot/adapters/__init__.py`` -- the WI-49 registry sits next to
    the existing portal adapter classes but uses a separate registration
    function so canvas gestures stay isolated from portal-wide read /
    write methods). Unknown adapter -> the runner emits
    ``error_kind='action_not_implemented'`` with the missing name in
    ``error_details`` so the operator sees exactly which adapter to
    register.

    Acceptance check (from the WI brief):
      A ``canvas_gesture`` step with ``adapter_name='noop_click'``
      records + replays. The sample noop_click adapter dispatches a
      click at the canvas center so the schema + dispatch path is
      tested end-to-end without requiring a real canvas portal.
    """

    adapter_name: str
    """Name of the registered canvas gesture adapter. Looked up via
    ``pilot.adapters.get_canvas_adapter(name)`` -- the runner builds
    the adapter instance lazily."""

    target_descriptor: dict[str, Any] = Field(default_factory=dict)
    """Adapter-specific target descriptor. Typical shape:
    ``{selector: '[data-testid="chart-canvas"]'}`` or
    ``{role: 'img', accessible_name: 'Timeline'}``. The adapter
    interprets the descriptor -- the runner just hands it through."""

    action_payload: dict[str, Any] = Field(default_factory=dict)
    """Structured intent the adapter realizes as pointer events.
    Typical shapes: ``{kind: 'click_center'}`` for the noop sample,
    ``{kind: 'draw_box', coords: [10, 10, 100, 100]}`` for a real
    portal's drawing canvas, ``{kind: 'scrub_to', value: 0.5}`` for a
    media timeline."""

    expected_postcondition: Optional[str] = None
    """Optional StepAssertion-kind name the runner verifies after the
    adapter dispatch completes (e.g. ``visible`` for a shape that
    should appear after a draw_box). None disables post-action
    verification. Audit-only today; the runner reads this off the
    spec but the full assertion path goes through step.assert_after."""


class DownloadSpec(BaseModel):
    """WI-45: spec for a ``download`` step.

    The grabber captures the click that triggers a browser download via
    one of two evidence paths:

      1. An anchor / button with a ``download`` attribute (HTML5
         download attribute), OR
      2. A click whose attributed ``network_response`` carried a
         ``Content-Disposition: attachment`` header (server-driven
         download).

    The annotator emits a ``download`` step (action='download') with
    this spec attached and folds the originating click into it. At
    replay the runner wraps the click in Playwright's
    ``page.expect_download()`` context manager, asserts the captured
    filename / mime / size against the declared expectations, saves the
    file under ``<sessions_dir>/<session_id>/downloads/<filename>``, and
    surfaces the saved path on the step result.

    Acceptance: a CSV export click records as a ``download`` step;
    replay produces the file under ``sessions/<id>/downloads/``.
    """

    filename_template: Optional[str] = None
    """Expected filename pattern. Substring match (case-insensitive) at
    replay time after param substitution. None means the runner accepts
    whatever filename the browser reports (audit-only)."""

    expected_mime: Optional[str] = None
    """Expected MIME type prefix (e.g. ``text/csv``,
    ``application/pdf``). The runner reads the saved file's extension
    via ``mimetypes`` and matches against this prefix. None disables
    the check."""

    expected_min_bytes: Optional[int] = None
    """Minimum acceptable file size. Guards against the download
    happening but the server returning an empty / error body. None
    disables the size floor."""

    expected_signal: Optional["NetworkExpectation"] = None
    """Optional network expectation the runner waits for in parallel
    with the download (e.g. the GET that streams the file). Used when
    the download is server-driven and we want to assert the matching
    request completed with a 2xx status. None when the download is a
    pure client-side blob (Content-Disposition not involved)."""


class SetSelectionSpec(BaseModel):
    """Specification for a multi-select reconciliation step.

    The recording captured the operator opening the picker, searching
    once per item, checking the box, and closing the picker. The
    annotator collapses that into one step with this spec, parameterized
    by a list (the desired set of items). At replay, the runner:

      1. Reads the *current* selected items from current_items_selector.
      2. Computes the diff vs the target list, modulated by ``mode``:
          - "replace": uncheck items not in target, check items in target
          - "add": only check items in target; leave others alone
          - "remove": only uncheck items in target
          - "preserve": no-op if any current items overlap target;
            otherwise behave as "add". Used for "keep what the operator
            already had if it's relevant."
      3. For each item to check: open picker (if needed), use
         search_template to filter, click the materialized checkbox.
      4. For each item to uncheck: open picker, click the materialized
         chip-x (or checkbox-template again -- toggle semantics).
      5. Close picker.
    """

    mode: Literal["replace", "add", "remove", "preserve"]
    param: str
    """Name of the list parameter. Resolved to ``list[str]`` at replay.

    id+label convention (2026-05-28): replay param VALUES are human
    LABELS (e.g. ``country=Zimbabwe``), NOT opaque ids. The runner
    resolves each label to its id via ``known_options`` (label->value,
    case-insensitive) for the equality assertion + diagnostics, and
    reaches the option by typing the LABEL into the search box and
    clicking the surfaced row whose visible text matches -- the id
    template is never required for the click. The operator never types
    ids."""

    known_options: list["OptionSnapshot"] = Field(default_factory=list)
    """id+label sprint (2026-05-28): the full universe of options seen
    at record time -- every option row that rendered during the
    multi-select interaction (including server-search results), as
    ``OptionSnapshot`` entries (value=id, label=label). Populated by the
    annotator by unioning every contributing event's
    ``TraceEvent.options_seen``. This is the SUPERSET; ``item_labels``
    (below) is the subset the operator actually SELECTED.

    Three consumers:
      1. Replay -- the runner builds a label->id map (case-insensitive)
         so a target LABEL resolves to its id for the equality
         assertion; reach-by-label still works for labels NOT in this
         set (unknown-label diagnostic + best-effort search).
      2. LLM annotation -- the id+label pairs feed semantic inference
         and alias generation.
      3. Planner -- the v2 param surfaces these labels so a future
         clarify step can offer the operator the seen options.
    Empty for legacy skills recorded before the wider option capture."""

    open_picker_fp: Optional[ElementFingerprint] = None
    """Click target to open the dropdown. Optional -- some pickers stay
    open between actions, in which case this can be omitted."""
    search_fp: Optional[ElementFingerprint] = None
    """Input inside the picker to filter the list. Optional -- some
    pickers don't have search and just display all options at once."""
    checkbox_template_fp: Optional[ElementFingerprint] = None
    """Checkbox template with a {item} placeholder in testid /
    element_id. Used to materialize the per-item click target."""
    commit_fp: Optional[ElementFingerprint] = None
    """Click to close/commit the picker. Optional."""
    current_items_selector: Optional[str] = None
    """CSS selector that returns the chips/badges of currently selected
    items at this moment. Used to read current state. Each match must
    carry a stable identifier we can map to a target item (typically
    via a data-* attribute on the chip). Example:
    ``[data-testid^='multiselect-categories-chip-']``."""
    current_items_id_attr: str = "data-testid"
    """Attribute on the matched chips that carries the item's id, used
    to compute the diff. The value is the testid string; we strip a
    known prefix to get just the item id."""
    current_items_id_prefix: Optional[str] = None
    """If set, strip this prefix from the chip's id attribute to get
    the raw item id. E.g. for testid='multiselect-categories-chip-sports',
    prefix='multiselect-categories-chip-' yields 'sports'."""

    final_equality_assertion: bool = True
    """WI-19: after the reconciliation, the runner reads the chip set
    again and verifies it equals the target set EXACTLY. ``True`` is
    safe-by-default for the WI-19 acceptance check ('recorded
    categories [sports, drama] replayed with [kids] ends with exactly
    [kids] chips'). When the operator declares ``False`` -- e.g. for
    ``mode='add'`` where they want to preserve other selections -- the
    runner skips the equality check and only verifies the diff applied.

    Set to ``False`` to opt out for legacy skills where the chip
    container selector is unreliable; ``True`` is the new safe default
    introduced by WI-19 that catches the silent set_selection_remove_
    failed / set_selection_commit_failed bugs WI-06 surfaced as warnings
    but did not fail-stop on."""

    depends_on: Optional[str] = None
    """WI-20: name of a parent picker param whose value drives this
    multiselect's option set. E.g. ``parent_category`` for a nested
    subcategory picker. When set, the runner ensures the parent picker
    has been set to the dependency's value (via a prior step or by
    reading the current state) BEFORE reconciling the child set.

    Used together with ``parent_picker_fp`` + ``option_source`` to
    describe a 'category -> subcategory' chain where changing the
    parent refreshes the child's option list."""

    parent_picker_fp: Optional[ElementFingerprint] = None
    """WI-20: fingerprint of the parent picker (select / autocomplete /
    multiselect) whose value drives this picker's options. Resolved
    against ``depends_on`` to fetch the parent value. None when no
    cascading parent (flat picker; this is the WI-19 case)."""

    option_source: Optional["NetworkExpectation"] = None
    """WI-20: the backend request the parent change is expected to fire
    to refresh THIS picker's options. Runner waits for this request to
    complete (action-baseline scoped per WI-09) before reconciling the
    child set so it never picks from stale options."""

    search_result_signal: Optional[str] = None
    """WI-20: URL substring of the search/filter request the picker
    fires while the operator types in the search field. Distinct from
    ``option_source`` (initial refresh) -- this is per-keystroke
    filtering. Runner waits for this signal after typing into the
    search field, before clicking the per-item checkbox."""

    # Real-portal cases A/B: reaching an option is replay-time logic, not
    # replayed keystrokes. The structural fact is "picker's selected set
    # == target list"; HOW each option is reached is chosen at replay for
    # robustness. These three fields carry the reach mechanism.
    item_labels: dict[str, str] = Field(default_factory=dict)
    """Map of item id -> display label. The runner types the LABEL (not
    the id) into the search box, because portal search matches on the
    visible option text. Root-cause bug fix (real-portal case B): the
    runner used to fill the search with the item id, which never matched
    a label-indexed server search. Empty map => the runner falls back to
    typing the raw item id (legacy behavior, preserved for skills
    recorded before this field existed). Populated by the annotator from
    each checkbox/item click's accessible_name / text / aria_label."""

    select_strategy: Literal["search", "scroll", "direct"] = "direct"
    """Replay-time reach mechanism for each option to add/remove:
      - ``search``: type the item's label into ``search_fp`` to narrow
        the list, wait for the materialized checkbox to become VISIBLE
        (bounded by wait_policy.dom_timeout_ms -- NOT a fixed sleep,
        which naturally rides out a server-side search spinner), click
        it, then clear the search for the next item. The annotator sets
        this whenever the picker exposes a search box.
      - ``scroll`` / ``direct``: resolve the checkbox via
        ``checkbox_template_fp`` then ``scroll_into_view_if_needed()``
        before clicking. Used for off-viewport targets in pickers
        without a search box (real-portal case A). The portal's lists
        are non-virtualized, so a locator + scroll is sufficient; no
        scroll-until-render loop is needed.
    Default ``direct`` preserves legacy behavior for skills recorded
    before this field existed. When ``select_strategy='search'`` but no
    ``search_fp`` is present, the runner falls back to scroll/direct;
    when ``direct``/``scroll`` can't find the checkbox AND ``search_fp``
    exists, the runner falls back to the search path."""

    option_list_selector: Optional[str] = None
    """CSS selector for the popover / listbox container. Two uses:
      1. Visibility scope -- the runner waits for a target checkbox to
         become visible (search strategy) and may scope the wait to this
         container so a stale duplicate elsewhere on the page can't
         satisfy it.
      2. Scroll fallback container -- the scroll-into-view target.
    Derived by the annotator from the picker prefix
    (``[data-testid='{prefix}-popover']``) or a scoped ``[role='listbox']``.
    None for legacy skills (the runner falls back to page-wide
    visibility + the locator's own scroll_into_view_if_needed)."""

    hierarchy_path: list[str] = Field(default_factory=list)
    """WI-20: for hierarchical pickers (e.g. category > subcategory >
    leaf), the path from root to this picker. Audit-only -- describes
    the structural relationship for operator review UI. Empty for flat
    pickers."""


class SkillStep(BaseModel):
    """One step of an operator demonstration."""

    index: int
    action: ActionType

    # Present for most actions; absent for navigate/wait
    fingerprint: Optional[ElementFingerprint] = None

    # Per-action payload
    url: Optional[str] = None                # navigate
    value: Optional[str] = None              # change / key
    file_path: Optional[str] = None          # upload
    wait_ms: Optional[int] = None            # wait

    # Annotation / semantics — filled during annotate step
    semantic_label: Optional[str] = None     # e.g. "enter_content_id"
    param_binding: Optional[ParamBinding] = None
    requires_gate: bool = False              # irreversible → human approval
    gate_reason: Optional[str] = None
    post_condition: Optional[PostCondition] = None

    # New (2026-05-21): targeted wait + declarative verification +
    # learning. Each field is optional and additive -- skills without
    # them keep working via the existing fallback heuristics.
    expected_signals: Optional[ExpectedSignals] = None
    """Per-step wait targets. Replaces generic in-flight-count wait
    when present -- the runner waits ONLY for these specific signals.
    Mostly populated by annotate-LLM observing the network activity
    during recording."""

    assert_after: list[StepAssertion] = Field(default_factory=list)
    """Declarative post-condition checks. Run after the action; failure
    flips the step to ``error_kind="post_condition_failed"`` and
    triggers recovery. Action-specific by design (don't AX-tree-diff)."""

    disambiguation_hint: Optional[DisambiguationHint] = None
    """Features from a prior operator resolution of ambiguous_target.
    Runner scores candidates against this before pausing again."""

    ambiguity_policy: Optional[AmbiguityPolicy] = None
    """WI-22: per-step ambiguity detection / policy config. None means
    'use defaults' -- which the runner translates to
    ``fail_if_multiple`` for destructive actions (requires_gate True or
    action in click/fill_submit/select_option/upload/select_autocomplete/
    set_selection) and ``prompt`` for non-destructive observations.
    Annotator emits this with the recorded ancestor chain as
    locator_scope when available."""

    repair_policy: Optional[RepairPolicy] = None
    """WI-26: per-step L3 heal acceptance policy. None means the
    runner uses defaults derived from the action's risk: destructive
    actions get medium_confidence_pauses=True; non-destructive get
    legacy execute-but-don't-persist. The annotator emits this from
    observed fingerprint quality (test_id present => test_id_required)
    and the action's risk. When set, the runner gates L3 heal
    acceptance on the declared policy BEFORE the score band check."""

    set_selection: Optional[SetSelectionSpec] = None
    """Spec for action='set_selection' steps. Carries the picker
    open/close fingerprints, the search/checkbox templates, and the
    reconciliation mode."""

    fill_submit: Optional[FillSubmitSpec] = None
    """WI-15: spec for action='fill_submit' steps. Carries the submit
    trigger ('enter' / 'button' / 'form_submit'), the value param
    binding, the optional submit button fingerprint, and the optional
    expected-result network signal. None for non-fill_submit actions."""

    select_autocomplete: Optional[AutocompleteSpec] = None
    """WI-16: spec for action='select_autocomplete' steps. Carries the
    separated query / selected_item params, the result container, the
    option identity template, and the network expectation that gates
    selection on the search response. None for non-autocomplete
    actions."""

    select_option: Optional[SelectOptionSpec] = None
    """WI-17: spec for action='select_option' steps. Carries the
    recorded value/label, the options snapshot, the match mode, and
    declared aliases. Replaces fuzzy-fallback with declared-or-fail
    semantics. None for non-select_option actions."""

    dependency_chain: Optional[DependencyChain] = None
    """WI-18: parent->child cascading-select declaration. Attached to
    the PARENT select step so the runner can wait for the child's
    options to refresh before the child step runs. None for
    non-cascading steps."""

    date_select: Optional[DatePickerSpec] = None
    """WI-21: spec for action='date_select' steps. Carries the
    native/custom kind, the value param, locale/timezone hints, and
    (for custom) calendar widget navigation fingerprints. None for
    non-date_select actions."""

    slider_set: Optional["SliderSpec"] = None
    """WI-28: spec for action='slider_set' steps. Carries the value
    param, the min/max/step constraints, orientation, and the event
    dispatch mode. None for non-slider actions."""

    shortcut: Optional["ShortcutSpec"] = None
    """WI-41: spec for action='shortcut' steps. Carries the modifiers
    + key + scope + expected_effect. The runner calls
    page.keyboard.press(combo) and waits for the declared effect.
    None for non-shortcut actions."""

    rich_text: Optional["RichTextSpec"] = None
    """WI-39: spec for action='rich_text_set' steps. Carries the value
    param, format (html/markdown/plain), embed policy, paste strategy,
    and editor framework hint. None for non-rich-text actions or for
    legacy traces. The runner uses the spec to dispatch through
    framework-specific APIs when the framework_hint matches, otherwise
    falls back to paste_strategy."""

    file_spec: Optional["FileSpec"] = None
    """WI-29: spec for ``upload`` (file_selected) steps. Carries the
    recorded file metadata + the input's accept attribute + the
    multiple flag, so the runner can validate the replay-time path
    BEFORE set_input_files runs. None for non-upload actions or for
    legacy traces (which lacked the WI-29 metadata)."""

    drag_drop: Optional["DragDropSpec"] = None
    """WI-30: spec for ``drag_drop`` steps. Carries the source +
    target fingerprints, the DataTransfer payload summary, drop
    effect, and coordinates policy. None for non-drag_drop actions."""

    download_spec: Optional["DownloadSpec"] = None
    """WI-45: spec for ``download`` action steps. Carries the
    filename / mime / size expectations and an optional network
    signal the runner waits for in parallel with the
    ``page.expect_download()`` context. None for non-download actions
    and for legacy traces (in which case a click + observed
    Content-Disposition response stayed a plain click rather than
    being folded into a download step)."""

    canvas_gesture: Optional["CanvasGestureSpec"] = None
    """WI-49: spec for ``canvas_gesture`` action steps. Carries the
    adapter name, target descriptor, structured action payload, and
    expected post-condition. None for non-canvas actions. The runner
    dispatches through the registered adapter; an unknown adapter falls
    back to ``error_kind='action_not_implemented'`` with a clear
    diagnostic."""

    toggle_state: Optional["ToggleStateSpec"] = None
    """WI-33: spec for ``toggle_state`` steps (accordion / expand-
    collapse). Carries the desired target_state, the state attribute
    to read, and the controlled panel selector. None for clicks that
    weren't classified as toggles."""

    scroll_until: Optional["ScrollUntilSpec"] = None
    """WI-37 + WI-38: spec for ``scroll_until`` steps. Carries the
    scroller fingerprint, target identity, max_scrolls, page-size
    hint, network signal, and direction. None for non-scroll_until
    actions. The runner dispatches wheel events against the scroller
    and re-probes the target's visibility between iterations until
    the target_visible_in_scroller DomExpectation passes or
    max_scrolls is exhausted."""

    strict_locale: bool = False
    """WI-48: when True, the runner FAILS this step with
    ``error_kind='locale_mismatch'`` if the replay-time locale /
    timezone differ from Skill.recording_context. Default False emits
    a warning diagnostic instead. Operators set True on steps whose
    correctness depends on locale-formatted input being read back the
    same way (e.g. a date filter that the portal stores as the
    rendered string)."""

    page_context: Optional["PageContext"] = None
    """WI-46: cross-tab / multi-window routing hint. When set, the
    runner resolves locators + dispatches actions for THIS step on
    ``BrowserSession.popup_pages[page_context.page_binding_key]``
    instead of the main session.page. None means the step runs on the
    main page (the legacy default). The annotator stamps this on
    steps that live INSIDE a popup workflow per WI-46."""

    auth_precondition: Optional["AuthPrecondition"] = None
    """WI-36: declarative auth gate the runner verifies BEFORE running
    the step. Populated by the annotator on steps whose folded network
    events show PATCH/POST/PUT/DELETE (destructive writes). The runner
    pauses with error_kind=``auth_missing`` when the portal's
    auth_signal probe returns ``missing`` -- the operator logs in and
    resumes. None for non-destructive steps and for portals without an
    auth_signal declared on context."""

    # WI-01: structured effects + replay policy + provenance. Each is
    # optional and defaults to None / a permissive default so legacy
    # v1 skills load and execute exactly as before.
    effects: Optional[StepEffect] = None
    """Consequences of this action (navigation / popup / modal /
    download / toast / etc.). Used by the runner to verify the action
    achieved what it should, instead of relying on a separate
    independent step that hardcodes the consequence."""
    replay_policy: ReplayPolicy = Field(default_factory=ReplayPolicy)
    """Per-step fail-fast / continue / optional policy. Default
    ``on_failure="abort"`` is intentional: the runner stops on the
    first failure unless a step explicitly opts into continuation."""
    provenance: Optional[StepProvenance] = None
    """How the annotator built this step from the raw trace. Empty
    for legacy traces; populated for new skills from WI-02 onward."""

    value_transition: Optional[ValueTransition] = None
    """WI-13: before/after value capture for change steps. When
    ``clear_intent=True`` the runner intentionally fills empty string
    and asserts the field reads empty post-action. Without this field
    the legacy annotator dropped empty inputs and lost
    "clear field and save" workflows."""

    click_gesture: Optional[Literal[
        "single", "double", "repeat", "toggle", "open", "close",
    ]] = None
    """WI-14: gesture classification on click steps.
      - ``single``  -- one click; standard Playwright .click().
      - ``double``  -- detail>=2 from the grabber; runner uses
                       .dblclick(). For grid cell edit / file open / etc.
      - ``repeat``  -- multiple successive clicks where the effect
                       differs (counter increment, page advance);
                       runner clicks N times in sequence.
      - ``toggle``  -- clicks that flipped a state attribute
                       (aria-expanded, aria-checked, aria-pressed).
                       Runner prefers a desired-state target over click
                       count (see ToggleStateSpec, WI-33).
      - ``open``    -- state transitioned from collapsed -> expanded
                       (or from hidden -> visible). Single-direction.
      - ``close``   -- expanded -> collapsed.
    None = legacy / unclassified; runner falls back to .click()."""

    effect_signature: Optional[dict[str, Any]] = None
    """WI-14: compact diff between target_state_before / target_state_
    after captured by the grabber. Lets the annotator distinguish
    'click did nothing' from 'click toggled state' without re-querying
    the DOM at replay. Typical shape: ``{aria_expanded: ['false',
    'true'], disabled: [null, 'true']}`` -- list of [before, after]."""

    # Debug / context
    captured_at: Optional[datetime] = None
    screenshot_path: Optional[str] = None
    auto_post_observed: Optional[dict[str, Any]] = None   # auto-observed DOM diff


class ParamConstraints(BaseModel):
    """WI-05: typed constraints checked by the runner BEFORE the step
    touches the page. Failing constraints emit
    ``error_kind="param_validation_failed"`` so the operator sees what
    was rejected and why rather than a downstream page error.

    Each field is optional; constraint checks short-circuit on the
    first hit. ``allowed_values`` is matched case-sensitively; aliasing
    belongs on the SelectOptionSpec, not here."""

    allowed_values: Optional[list[str]] = None
    """Whitelist of acceptable values. Used by ``enum`` params plus any
    other type where the operator wants strict membership. Compared
    after codec resolution: an ``enum_label`` codec converts a recorded
    label to its value before this check."""
    min: Optional[float] = None
    max: Optional[float] = None
    """Numeric bounds. Applied to ``number`` / ``number_range`` /
    ``date`` / ``datetime`` after codec resolution (dates compare as
    iso strings -- ``min='2025-01-01'`` accepts any later iso date)."""
    mime_types: Optional[list[str]] = None
    """Whitelist of MIME prefixes (e.g. ``image/``, ``application/pdf``)
    for ``file_path`` params. The runner reads the file's extension and
    Python's ``mimetypes`` module; the page-side ``accept`` attribute is
    already enforced by the browser at upload, this check fails fast."""
    extensions: Optional[list[str]] = None
    """Lowercased file extensions (with leading dot, e.g. ``.png``).
    Applied to ``file_path`` after ``mime_types``."""
    required_shape: Optional[str] = None
    """Regex (Python ``re``) the resolved value must fully match. Cheap
    safety net for portals that demand specific id/sku formats. Compiled
    once per validation; failures emit the offending regex in
    ``error_details``."""
    list_min_len: Optional[int] = None
    list_max_len: Optional[int] = None
    """Bounds on ``string_list`` length. ``list_max_len=1`` enforces
    'single item passed as a list' for portals whose set_selection
    happens to accept only one item per call."""


# WI-05: typed param expansion.
#
# Legacy v1 skills only declared ``string`` / ``number`` / ``date`` /
# ``file_path`` / ``string_list``. They worked because the runner stored
# everything as a string and the page accepted whatever showed up. That
# pushed semantic decisions (date format, boolean coercion, enum
# membership) into per-action shim code in skill_runner.py -- the same
# kind of hidden default the audit flagged.
#
# These types are paired with a ``codec`` that converts the operator-
# provided value to the canonical string the page expects (ISO date,
# localized number, enum value, file path resolved against the session).
# The runner runs codec -> constraints in that order, then hands the
# resolved value to the action handler. Bad values fail BEFORE the page
# mutates, with structured error_kind="param_validation_failed".
SkillParamType = Literal[
    # Legacy v1 types preserved for backward compatibility.
    "string",
    "number",
    "date",
    "file_path",
    "string_list",
    # WI-05 typed expansion.
    "boolean",           # checkboxes / toggle switches
    "enum",              # native single-select with a fixed option set
    "number_range",     # range slider / numeric with declared min/max
    "datetime",          # iso datetime with optional timezone
    "file_path",         # legacy; codec carries the resolution policy
    "object",            # nested JSON payload (rich text, structured form)
    "file_path_list",   # WI-29: multiple-file upload (input multiple)
]


SkillParamCodec = Literal[
    "raw",              # pass-through. Default for ``string`` params.
    "iso_date",         # parse + canonicalize to ``YYYY-MM-DD``
    "iso_datetime",     # parse + canonicalize to RFC 3339
    "localized_number",  # parse with locale_hint, emit canonical decimal
    "enum_value",       # operator passed the option ``value`` directly
    "enum_label",       # operator passed the option ``label``; codec
                        # converts to value via the captured options_snapshot
    "file_ref",         # ``file_path`` resolved against session artifact
                        # dir (and validated for existence)
    "boolean",          # accept true/false/1/0/on/off; normalize to "true"/"false"
]


class SkillParam(BaseModel):
    """Declared parameter of a skill."""

    name: str
    type: SkillParamType = "string"
    description: str = ""
    example: Optional[str] = None
    required: bool = True

    depends_on: Optional[str] = None
    """Another param this one depends on. The runner uses this signal
    in two ways:
      (1) At replay, the dependent param's value is selected from the
          CURRENT dropdown options (not the recorded one), because the
          dropdown contents change based on the dependency's value
          (Country -> State, Region -> Market).
      (2) For wait scheduling: the dependent's network call won't fire
          until the dependency's call has completed.
    None means independent."""

    select_from_current_options: bool = False
    """Force "ignore recorded value, pick from current options" behavior
    even for non-dependent params. Useful when the option set is
    inherently dynamic (e.g. asset status transitions)."""

    # WI-01: provenance for the param value. None for legacy auto-named
    # params; populated by future deterministic annotation pass (WI-11).
    provenance: Optional[ParamProvenance] = None

    # WI-05: typed codec + constraints.
    codec: SkillParamCodec = "raw"
    """How the operator-provided value is converted to the canonical
    string the page expects. Default ``raw`` is pass-through so legacy
    string params behave identically. Specific codecs:
      - ``iso_date``: input may be ``M/D/YYYY``, ``YYYY-MM-DD``, etc.;
        codec emits ``YYYY-MM-DD``.
      - ``localized_number``: parses with locale (read off param
        provenance / PortalContext locale_hint); emits canonical decimal.
      - ``enum_value`` / ``enum_label``: bind to a SelectOptionSpec on
        the same step's fingerprint.options_snapshot.
      - ``file_ref``: resolves against the session artifact dir,
        validates existence."""

    constraints: Optional[ParamConstraints] = None
    """Optional typed constraints. The runner validates after codec
    resolution -- failing values raise error_kind=
    ``param_validation_failed`` before the page is touched. ``None``
    means no constraints declared (legacy + raw string params)."""

    enum_options: Optional[list[OptionSnapshot]] = None
    """For ``enum`` params: the option set captured at record time,
    used by the ``enum_label`` codec to map labels back to values and by
    the runner to fail with a useful list when an operator-passed value
    isn't in the set. Populated by the annotator from the recording's
    ElementFingerprint.options_snapshot."""

    enum_aliases: dict[str, list[str]] = Field(default_factory=dict)
    """WI-25: operator-declared aliases for enum params.

    Key is the recorded / canonical value. Value is the list of
    acceptable alternative values OR labels the runner will accept as
    matching the canonical. E.g. ``{"US": ["USA", "United States"]}``.

    Aliases NEVER come from text similarity / annotator fuzzy
    inference; they're either typed in by the operator OR mirrored
    from a PortalContext declaration. The runner refuses to auto-fuzzy
    when match_mode is anything other than ``legacy_fuzzy`` -- the
    audit-flagged "US auto-matches UAE" failure mode is structurally
    impossible under match_mode=exact_only / alias / current_options.

    Same shape as SelectOptionSpec.aliases; declaring on the
    SkillParam lets the alias map apply to every select_option step
    that binds this param without copy-paste."""


# ---------------------------------------------------------------------------
# WI-08 migration: legacy click + navigate -> click with navigation effect
#
# Existing skills recorded BEFORE WI-08 emit two separate steps when the
# operator clicked something that triggered a SPA route change: one
# click step + one navigate step with the literal URL. The runner then
# called page.goto() with that URL on replay, which (per BLOCKER 1)
# reloaded the SPA at the recorded asset id instead of the current one
# -- the wrong-asset bug.
#
# This upgrader scans a list of step dicts (raw JSON) for consecutive
# click->navigate pairs whose timestamps are close together and lifts
# the navigate's URL into the click's effects.navigation.url. The
# standalone navigate step is dropped. Idempotent: runs as part of
# Skill.model_validate() so existing skills load with the upgrade
# applied; new skills (already structured by the annotator) pass
# through unchanged.
#
# Choice documented in WI-08 spec: we upgrade at load time silently
# rather than requiring re-recording. The rationale is that the legacy
# behavior is structurally wrong (the audit's release blocker) and the
# new behavior is strictly safer (click + assert vs click + goto). An
# operator who wants the legacy path back can declare
# replay_policy.reload_allowed=True on the upgraded step.
# ---------------------------------------------------------------------------


def _derive_legacy_url_template(
    nav_url: str,
    prior_steps: list[dict[str, Any]],
) -> Optional[str]:
    """Walk prior steps' click fingerprint templates / value bindings
    to find a (param_name, recorded_value) pair whose value appears in
    nav_url. Substitute the longest matching value to produce a
    template like ``/asset/{content_id}``.

    Returns None when no substitution applies -- the literal URL stays
    as the assert target. Conservative threshold: ignore values < 3
    chars to avoid accidental matches (``A-`` matching anywhere).
    """
    pairs: list[tuple[str, str]] = []
    for s in prior_steps:
        if not isinstance(s, dict):
            continue
        binding = s.get("param_binding") or {}
        name = binding.get("name") if isinstance(binding, dict) else None
        if not name:
            continue
        val = s.get("value") or s.get("file_path") or ""
        if not val or len(str(val)) < 3:
            continue
        pairs.append((name, str(val)))
        # Also harvest fingerprint templates (substring matches on test_id /
        # etc.) -- those expose the param values without needing the
        # operator to have filled a separate input first.
        fp = s.get("fingerprint") or {}
        templates = fp.get("templates") or {}
        for field, tmpl in templates.items():
            literal = fp.get(field)
            if not literal or not isinstance(tmpl, str):
                continue
            # The recorded literal contains the value we want; extract
            # by reversing the template substitution. ``{name}`` ->
            # value (single param assumption).
            placeholder = "{" + name + "}"
            if placeholder in tmpl:
                head_tail = tmpl.split(placeholder, 1)
                if (
                    len(head_tail) == 2
                    and literal.startswith(head_tail[0])
                    and literal.endswith(head_tail[1])
                ):
                    extracted = literal[
                        len(head_tail[0]) : len(literal) - len(head_tail[1])
                        if head_tail[1]
                        else len(literal)
                    ]
                    if extracted and len(extracted) >= 3:
                        pairs.append((name, extracted))
    if not pairs:
        return None
    # WI-08 conservative templater: only substitute when the recorded
    # value is the WHOLE final URL path segment. This rejects the
    # substring-luck pattern (search query 'A-90' becoming part of
    # '/asset/A-9001' as '/asset/{q}01'). True provenance-based
    # templating (route param + selected row key + request body) ships
    # in WI-11; until then we'd rather leave the URL literal than
    # produce a wrong template.
    try:
        # Extract final path segment, stripping query/hash.
        path = nav_url.split("?", 1)[0].split("#", 1)[0]
        last_seg = path.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        return None
    if not last_seg or len(last_seg) < 3:
        return None
    # Find a recorded value that EXACTLY equals the last segment.
    for name, value in pairs:
        if value == last_seg:
            return nav_url.replace(last_seg, "{" + name + "}", 1)
    return None


def _upgrade_legacy_click_nav_pairs(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse adjacent click + navigate pairs into click + navigation
    effect. Operates on raw step dicts (pre-model_validate) so it can
    run during Skill validation without recursive model construction.
    """
    if not steps or len(steps) < 2:
        return steps
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(steps):
        s = steps[i]
        next_s = steps[i + 1] if i + 1 < len(steps) else None
        if (
            isinstance(s, dict)
            and isinstance(next_s, dict)
            and s.get("action") == "click"
            and next_s.get("action") == "navigate"
            and not (s.get("effects") or {}).get("navigation")
            and next_s.get("url")
        ):
            nav_url = str(next_s.get("url") or "")
            # WI-08: try to template the URL by walking prior steps for
            # bound param values that appear as substrings.
            url_template = _derive_legacy_url_template(nav_url, out + [s])
            # Build the navigation effect inline. ``reload_allowed`` is
            # False so the runner doesn't fall back to page.goto -- the
            # whole point of WI-08 is to remove the second goto.
            click_with_eff = dict(s)
            effects = dict(s.get("effects") or {})
            effects["navigation"] = {
                "kind": "spa_route",
                "url": nav_url,
                "url_template": url_template,
                # WI-11: legacy upgrader can't reach the recording's
                # ancestor chain, so the strongest source it can claim
                # is ``legacy_substring`` (best-effort exact-segment
                # match performed by _derive_legacy_url_template).
                "url_template_source": (
                    "legacy_substring" if url_template else None
                ),
                "source": "legacy_upgrade",
                "reload_allowed": False,
                "assert_url": url_template or nav_url,
            }
            click_with_eff["effects"] = effects
            # Carry both raw event ids forward in provenance.
            prov = dict(click_with_eff.get("provenance") or {})
            raw_ids = list(prov.get("raw_event_ids") or [])
            next_prov = next_s.get("provenance") or {}
            for nid in next_prov.get("raw_event_ids") or []:
                if nid not in raw_ids:
                    raw_ids.append(nid)
            if raw_ids:
                prov["raw_event_ids"] = raw_ids
                prov["cluster_kind"] = "click_with_navigation"
                prov["detection_method"] = "migration"
                click_with_eff["provenance"] = prov
            out.append(click_with_eff)
            # Skip the standalone navigate step entirely.
            i += 2
            continue
        out.append(s)
        i += 1
    return out


class RecordingContext(BaseModel):
    """WI-48: locale + timezone snapshot taken at recording time.

    The grabber captures ``document.documentElement.lang`` and
    ``Intl.DateTimeFormat().resolvedOptions().timeZone`` once per
    session and the annotator stamps the result onto ``Skill.recording_context``.

    The runner reads this at replay-start to compare against the
    current page's locale / timezone:
      - If they differ AND any step declares ``strict_locale=True``,
        the runner FAILS the step with ``error_kind='locale_mismatch'``.
      - Otherwise the runner emits a ``locale_mismatch`` warning
        diagnostic (audit-only) and proceeds. The codec layer's
        ``iso_date`` / ``localized_number`` paths handle the
        format-then-parse normalization so the page receives the
        value its native locale expects.
    """

    locale: Optional[str] = None
    """BCP-47 locale tag (e.g. ``en-US``, ``de-DE``) observed at
    recording time. None when the grabber couldn't read it (page had
    no lang attribute and Intl wasn't available)."""

    timezone: Optional[str] = None
    """IANA timezone name (e.g. ``America/New_York``,
    ``Europe/Berlin``) from Intl. None when Intl wasn't available."""


class Skill(BaseModel):
    """A learned, parameterized, replayable skill."""

    name: str
    description: str = ""
    portal: Optional[str] = None                 # e.g. "sample_portal"
    tags: list[str] = Field(default_factory=list)
    version: int = 1
    # WI-01: structural-features schema version. ``1`` (default) means
    # legacy v1 skills with original ActionType set + no StepEffect /
    # ReplayPolicy / provenance. ``2`` indicates a skill built with the
    # post-WI-01 annotator and that the new fields may be populated.
    # Pydantic accepts missing fields = default for old files, so v1
    # skills load with schema_version=1 automatically.
    schema_version: int = 1
    upgraded_from: Optional[int] = None
    """WI-50: audit breadcrumb stamped by ``pilot.skill_upgrade.upgrade_skill_to_v2``
    when a legacy v1 skill is migrated. ``None`` means "natively this
    schema_version" (either a freshly recorded v2 or an unmodified v1).
    A non-None value records the version we upgraded FROM, so the audit
    log can distinguish 'this skill was hand-written at v2' from 'this
    skill was a v1 recording silently migrated at load'. Persisted in
    the JSON; re-upgrading a v2 that already carries the marker does
    not overwrite it."""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    base_url: Optional[str] = None

    params: list[SkillParam] = Field(default_factory=list)
    steps: list[SkillStep]

    # Provenance
    source_session_id: Optional[str] = None

    # WI-08: track whether the loaded skill was upgraded from a legacy
    # click+navigate pair so the audit log + tests can verify the
    # migration fired. Not persisted -- ``model_validator(mode='before')``
    # mutates the input dict in place, the audit reads this attribute
    # off the constructed Skill.
    legacy_nav_upgrade_count: int = 0
    """WI-08 migration counter. Non-zero means the loaded JSON had
    consecutive click + standalone navigate steps that were collapsed
    into click + effects.navigation at validate time. The runner /
    audit log can surface this to the operator."""

    annotate_mode: Literal["linear", "semantic"] = "semantic"
    """WI-12: which annotator pipeline produced this skill. ``linear``
    is the legacy event-to-step path preserved for back-compat with
    pre-WI-12 traces. ``semantic`` (default for new annotations) runs
    the full cluster pipeline (normalize -> causality -> cluster ->
    bind params -> produce steps -> emit signals). The runner does
    not branch on this field; it's informational + audit. Legacy
    skills loaded from JSON default to ``semantic`` unless explicitly
    written -- safe because the semantic pipeline degrades to one
    cluster per event when no widget pattern is detected."""

    recording_context: Optional["RecordingContext"] = None
    """WI-48: locale + timezone observed at recording time. Captured
    from the page's ``document.documentElement.lang`` and the browser's
    ``Intl.DateTimeFormat().resolvedOptions().timeZone``. Used by the
    runner to detect ``locale_mismatch`` when replay runs under a
    different locale / timezone, and by the codec layer to normalize
    date / number params through ``iso_date`` / ``localized_number``
    instead of relying on the page's native rendering. None for legacy
    traces (pre-WI-48) -- the runner treats absence as 'no
    expectation, no mismatch warning'."""

    semantic_clusters: list["SemanticCluster"] = Field(default_factory=list)
    """WI-12: structured cluster intermediate representation, retained
    alongside the materialized SkillSteps. Empty for legacy ``linear``
    skills and for skills loaded without re-annotation. Audit log +
    LLM enrichment (WI-31) consume this to reason about cluster
    boundaries / detection alternatives without re-deriving from raw
    events."""

    @model_validator(mode="before")
    @classmethod
    def _wi08_upgrade(cls, data: Any) -> Any:
        """WI-08: silently upgrade legacy click + navigate pairs at
        load time. Operates on the raw input dict so the produced
        Skill carries the new shape uniformly.

        Idempotent: a skill that already has effects.navigation on its
        clicks passes through unchanged."""
        if not isinstance(data, dict):
            return data
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            return data
        before = len(raw_steps)
        upgraded = _upgrade_legacy_click_nav_pairs(raw_steps)
        if len(upgraded) != before:
            # Re-index the surviving steps so SkillStep.index stays
            # sequential 0..N-1. Removing the standalone navigate
            # leaves a hole otherwise.
            for new_idx, s in enumerate(upgraded):
                if isinstance(s, dict):
                    s["index"] = new_idx
            data["steps"] = upgraded
            data["legacy_nav_upgrade_count"] = before - len(upgraded)
        return data

    def to_puppeteer_replay(self) -> dict[str, Any]:
        """Down-convert to vanilla Puppeteer Replay JSON format."""
        replay_steps: list[dict[str, Any]] = []
        for s in self.steps:
            if s.action == "navigate":
                replay_steps.append({"type": "navigate", "url": s.url or ""})
            elif s.action in ("click", "change", "submit", "upload"):
                selectors = _build_replay_selectors(s.fingerprint)
                entry: dict[str, Any] = {"type": _replay_type_for(s.action), "selectors": selectors}
                if s.action == "change" and s.value is not None:
                    entry["value"] = s.value
                replay_steps.append(entry)
            elif s.action == "key":
                replay_steps.append(
                    {"type": "keyDown", "key": s.value or ""}
                )
            elif s.action == "wait":
                replay_steps.append(
                    {"type": "waitForElement", "selectors": []}
                )
        return {"title": self.name, "steps": replay_steps}


def _replay_type_for(action: ActionType) -> str:
    mapping = {
        "click": "click",
        "change": "change",
        "submit": "click",   # Replay has no submit; a click on submit button suffices
        "upload": "change",
    }
    return mapping.get(action, "click")


def _replay_escape_attr(value: str) -> str:
    """WI-24: escape a value for CSS attribute selectors.

    Replaces the pre-WI-24 ``[attr='value']`` interpolation that broke
    on ``]``, ``'``, spaces, colons, and non-ASCII characters. We
    backslash-escape backslash and double-quote and emit the value
    inside double quotes. This shape is accepted by Puppeteer Replay's
    selector resolver and by Playwright's CSS engine. The runner uses
    Playwright's semantic locator APIs at execution; this exporter is
    for the down-converted Replay JSON.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_replay_selectors(fp: Optional[ElementFingerprint]) -> list[list[str]]:
    """Emit Puppeteer Replay selector-array-of-arrays from a fingerprint."""
    if fp is None:
        return []
    out: list[list[str]] = []
    if fp.test_id:
        # WI-24: double-quoted + escaped so values containing ``]`` /
        # quotes / spaces / colons / non-ASCII chars resolve correctly.
        out.append([f'[data-testid="{_replay_escape_attr(fp.test_id)}"]'])
    if fp.element_id:
        # WI-24: use [id="..."] attribute selector instead of #id
        # because #id rejects characters CSS treats as syntax (``:``,
        # ``.``, ``[``, etc.) and requires CSS.escape -- the attribute
        # form sidesteps the issue.
        out.append([f'[id="{_replay_escape_attr(fp.element_id)}"]'])
    if fp.accessible_name and fp.role:
        out.append([f"aria/{fp.accessible_name}"])
    if fp.css_path:
        out.append([fp.css_path])
    if fp.xpath:
        out.append([f"xpath//{fp.xpath}"])
    return out


# ----- Raw trace (pre-annotation) ------------------------------------------


class TraceEvent(BaseModel):
    """A single raw event captured by grabber.js during teach mode.

    This is the shape that arrives over Runtime.addBinding. After the
    session ends, the annotator converts a filtered list of these into
    SkillStep objects.
    """

    ts: datetime = Field(default_factory=datetime.utcnow)
    kind: Literal[
        # User-initiated events
        "click",
        "input_change",
        "submit",
        "file_selected",
        "navigate",
        "key",
        # WI-30: drag/drop user actions. dragstart anchors the
        # sequence; dragover is sampled (not every dragover -- the
        # grabber emits at most one per (target_id, 100ms) bucket);
        # drop carries the final landing target + DataTransfer
        # summary.
        "dragstart",
        "dragover",
        "drop",
        # WI-42: toast / snackbar appeared. Emitted by the grabber's
        # role=status / role=alert / known toast-container watcher
        # during a user interaction window. Carries toast_level (info
        # / success / warning / error inferred from class names + role)
        # and toast_text + optional action_button_test_id. The
        # annotator folds toasts into the causing step's
        # effects.toast (ToastEffect).
        "toast",
        # WI-40: hover that revealed a submenu. Emitted by the grabber
        # when a pointerenter / mouseover fires on a menu-trigger
        # element AND a subsequent DOM mutation makes a submenu visible
        # within a small window. We don't capture every hover (most are
        # incidental noise); only hovers that caused a visibility
        # transition are emitted. The annotator pairs the hover with
        # the subsequent click on a child item and folds both into ONE
        # click step with a HoverEffect.
        "hover",
        # F-07 / F-09: observed page-side events. network_request and
        # network_response carry request_id + method + url + status +
        # initiator_event_id so WI-09 / WI-10 / WI-43 / WI-47 can wait
        # on specific requests rather than scanning a global counter.
        # dom_mutation is a debounced burst summary for DOM-change-driven
        # waits. popup / download / visibility_change cover WI-35 / WI-45
        # / WI-46 cross-tab + observable-side-effect workflows.
        "network_request",
        "network_response",
        "dom_mutation",
        "popup",
        "download",
        "visibility_change",
        # WI-34: dialog mount/unmount observed by the grabber's
        # role=dialog MutationObserver. ``modal`` events carry a
        # dialog_state ('open' or 'closed'), a dialog_selector (the
        # stable selector the runner can re-resolve to), and the
        # initiator_event_id when emitted during a user interaction
        # window (e.g. clicked Publish -> dialog opens).
        "modal",
    ]
    fingerprint: Optional[ElementFingerprint] = None
    value: Optional[str] = None
    url: Optional[str] = None
    file_name: Optional[str] = None
    page_url: str = ""
    screenshot_path: Optional[str] = None
    dom_diff: Optional[dict[str, Any]] = None
    value_before: Optional[str] = None
    """WI-13: the input/textarea/contenteditable value AT THE MOMENT the
    operator started editing this field (typically on focus / first
    keystroke). Pairs with ``value`` (the after-value) so the annotator
    can detect a CLEAR (value_before='Old', value='') and route
    runner replay to ``locator.fill('')`` + post-assert empty. None for
    legacy traces and for non-text events (clicks, key, navigate)."""

    # WI-14: click gesture + effect classification. Populated by the
    # grabber on click events; consumed by the annotator to decide
    # single vs double-click vs toggle vs no-op, and by the runner to
    # pick the correct Playwright API (click() vs dblclick()).
    click_detail: Optional[int] = None
    """``MouseEvent.detail`` -- the system-reported click count.
    1 = single, 2 = double, 3 = triple. Set on click events only.
    None for legacy traces (the grabber didn't capture it pre-WI-14)."""
    pointer_type: Optional[str] = None
    """``PointerEvent.pointerType`` when available (``mouse`` / ``pen``
    / ``touch``). Distinguishes a finger tap from a mouse click; some
    portals listen specifically for touch vs mouse."""
    target_state_before: Optional[dict[str, Any]] = None
    """Snapshot of the click target's state immediately BEFORE the
    click: ``{aria_expanded?, aria_checked?, aria_pressed?, disabled?,
    selected?}``. Used by the annotator to detect a toggle (state
    flipped) vs a no-op (state unchanged) vs an open/close (single-
    direction expand/collapse)."""
    target_state_after: Optional[dict[str, Any]] = None
    """Same shape as target_state_before, captured on the microtask
    after the click. The DIFF between before and after is the
    ``effect_signature`` the annotator stamps onto the step."""

    # WI-02: identity + causality + ordering. Populated by the grabber
    # for new recordings; missing for legacy traces (Pydantic defaults
    # carry None / 0). The annotator builds a causality graph from
    # these instead of guessing from adjacency + time gaps.
    event_id: Optional[str] = None
    """Stable per-event ID assigned by the grabber (crypto.randomUUID()
    in the browser). Used as the target of ``caused_by`` references."""
    interaction_id: Optional[str] = None
    """Groups all events caused by a single user interaction. A click
    that triggers a SPA route change AND a network call AND a DOM
    mutation produces one click event + N effect events sharing this
    id. The annotator uses interaction_id to collapse effect events
    into the causing step's ``effects`` field."""
    caused_by: Optional[str] = None
    """``event_id`` of the event that caused THIS event. For the click
    itself, ``caused_by`` is None. For a navigate emitted by React
    Router after the click, ``caused_by`` = click's event_id. For
    network requests started inside an interaction, caused_by is the
    triggering user event."""
    sequence: Optional[int] = None
    """Monotonic order within the session. Lets the annotator
    reconstruct ordering even if ts has clock skew or two events share
    the same millisecond."""
    source: Optional[str] = None
    """Where the event originated. Examples: ``user_click``,
    ``user_keydown``, ``user_submit``, ``history.pushState``,
    ``history.replaceState``, ``popstate``, ``hashchange``,
    ``fetch_request_start``, ``fetch_response``, ``xhr_loadend``,
    ``mutation_observer``, ``visibility_change``."""
    monotonic_ts: Optional[float] = None
    """``performance.now()`` from the page. More accurate than wall-
    clock ``ts`` for ordering tiny intervals."""
    raw_event_kind: Optional[str] = None
    """The DOM event name behind a translated ``kind``. E.g.
    kind=``input_change``, raw_event_kind=``input``. Useful when the
    annotator needs to distinguish ``input`` (during typing) from
    ``change`` (commit)."""
    page_state_before: Optional[dict[str, Any]] = None
    """Snapshot of URL/title/key DOM state immediately before the
    event. Used by the annotator to verify caused-state changes."""
    page_state_after: Optional[dict[str, Any]] = None
    """Same, after the event resolved."""

    # F-07: observed-event payload fields. These are populated only on
    # the observed kinds (network_request, network_response,
    # dom_mutation, popup, download, visibility_change). They live on
    # TraceEvent (rather than a separate envelope) so the annotator can
    # walk a single ordered list when building the causality graph.
    request_id: Optional[str] = None
    """Stable per-request ID assigned by the grabber's fetch/XHR hook.
    Links a network_request event to its matching network_response.
    Used by WI-09 expected_signals.started_after_event so waits key off
    a specific request, not a substring match against a global log."""
    method: Optional[str] = None
    """HTTP method (GET/POST/PUT/PATCH/DELETE) of a request event."""
    started_at: Optional[float] = None
    """``performance.now()`` at fetch/XHR start. Lets WI-09 enforce
    'this request started AFTER the action's baseline' so a stale prior
    request can't accidentally satisfy a new step's expected_signal."""
    finished_at: Optional[float] = None
    """``performance.now()`` at fetch/XHR finish. None until the request
    settles. Pairs with ``status`` on network_response events."""
    status: Optional[int] = None
    """HTTP response status. Populated on network_response events."""
    initiator_event_id: Optional[str] = None
    """``event_id`` of the user-action event that triggered this
    request, when the grabber can infer it (set inside the synchronous
    handler of a click / submit / key). None for requests not
    attributable to a specific user action (e.g. background polling)."""
    mutation_summary: Optional[dict[str, Any]] = None
    """For dom_mutation events: a compact summary of the debounced
    mutation burst -- counts of added/removed/attribute nodes plus the
    nearest stable ancestor selector for the most-changed subtree. Lets
    WI-09 / WI-10 detect 'this action caused the DOM to actually
    change' without scanning every individual mutation record."""

    file_metadata: Optional[list[FileMetadata]] = None
    """WI-29: per-file metadata on ``file_selected`` events. List of
    {name, size, mime, ext} records for every file the operator
    picked. None for legacy traces (which only captured ``file_name``);
    annotator falls back to a single FileMetadata derived from
    ``file_name`` in that case."""

    options_seen: Optional[list[OptionSnapshot]] = None
    """id+label sprint (2026-05-28): the full set of option rows
    rendered inside a custom multi-select at the moment of this event.
    Populated by the grabber on multiselect option ``click`` and
    multiselect-search ``input_change`` events -- each entry is
    ``{value=id, label=visible label}``. Distinct from the native
    ``<select>`` ``options_snapshot`` (which lives on the fingerprint);
    this is the custom-widget universe captured ACROSS events
    (including server-search results as they surface). The annotator
    unions every event's ``options_seen`` for a picker into the
    SetSelectionSpec's ``known_options`` (label->id resolution at
    replay, LLM annotation, planner clarify). None for non-multiselect
    events and legacy traces."""

    drop_target_fp: Optional[ElementFingerprint] = None
    """WI-30: on ``drop`` events, the fingerprint of the target the
    user released over. Distinct from ``fingerprint`` (which is the
    dragged source on dragstart). The annotator pairs the dragstart's
    fingerprint with this drop_target_fp to build the DragDropSpec."""
    data_transfer_summary: Optional[dict[str, Any]] = None
    """WI-30: compact summary of the DataTransfer at drop time --
    {types: [...], text_plain: 'first 200 chars or null',
    drop_effect: 'move'|'copy'|'link'|'none'}. None for legacy traces
    and for events the grabber couldn't snapshot."""

    # WI-34: dialog mount/unmount payload. Populated only on
    # kind='modal' events emitted by the grabber's role=dialog
    # MutationObserver.
    dialog_state: Optional[Literal["open", "closed"]] = None
    """For ``modal`` events: ``open`` when a dialog mounted (added to
    the DOM or visibility became true) during the active user
    interaction; ``closed`` when it unmounted / hidden."""
    dialog_selector: Optional[str] = None
    """For ``modal`` events: a stable selector the runner can re-resolve
    to. Prefers ``[data-testid="..."]`` when the dialog carries one;
    falls back to ``[role="dialog"]`` with a count index when multiple
    are present."""
    dialog_aria_modal: Optional[bool] = None
    """For ``modal`` events: aria-modal attribute observed on the
    dialog at mount time. True for modals that trap focus (the usual
    case); False / None for non-modal dialogs."""

    # WI-35: popup / new-window payload. Populated on kind='popup'
    # events emitted by the grabber's window.open hook.
    popup_url: Optional[str] = None
    """For ``popup`` events: the URL the popup was opened to (the
    first argument to window.open). May be empty/null when the popup
    opened about:blank and was then navigated by the opener."""
    popup_target: Optional[str] = None
    """For ``popup`` events: the window.open() target argument
    (``_blank`` / window name / etc.). None for target=_blank link
    clicks where the grabber inferred the popup from the click handler
    rather than a window.open call."""
    popup_features: Optional[str] = None
    """For ``popup`` events: the window.open() features argument
    (``width=800,height=600,...``). Audit-only; the runner doesn't
    replay window features."""
    popup_binding_key: Optional[str] = None
    """For ``popup`` events: the page key the grabber assigned for
    cross-page step routing. The annotator stamps this onto the
    PopupEffect so the runner knows which page key to switch to."""

    # WI-45: download intent payload. Populated on kind='download'
    # events emitted by the grabber's download-attribute / data-
    # download-filename click watcher. The annotator folds these onto
    # the causing click step (the step's action becomes ``download``
    # and a DownloadSpec is attached).
    download_filename: Optional[str] = None
    """For ``download`` events: the declared filename hint from the
    ``download`` attribute or ``data-download-filename``. None when
    the attribute was empty (``<a download>`` without a value -- the
    browser will use the URL's basename)."""
    download_href: Optional[str] = None
    """For ``download`` events: the anchor's ``href`` URL when the
    download was anchor-driven. None for button-driven downloads."""

    # WI-37 / WI-38: scroll payload. Populated on kind='visibility_change'
    # events emitted by the grabber's scroll observer when an operator
    # scrolled a scroller and a previously-hidden row came into view.
    scroller_selector: Optional[str] = None
    """For scroll-driven ``visibility_change`` events: the scroller's
    selector (the element that received the scroll). Used by WI-37/38
    to detect 'operator scrolled to find a row' patterns."""
    scroll_direction: Optional[Literal["up", "down", "left", "right"]] = None
    """For scroll-driven ``visibility_change`` events: which way the
    operator scrolled. ``down`` is the most common; ``up`` indicates
    the operator overshot then came back."""
    visible_row_count_delta: Optional[int] = None
    """For scroll-driven ``visibility_change`` events: change in the
    count of visible rows inside the scroller after the scroll. Used
    by the scroll detector to confirm the scroll caused new rows to
    render (positive delta) rather than just shifting the viewport
    within already-rendered content (zero delta)."""

    # WI-42: toast / snackbar payload. Populated on kind='toast' events
    # emitted by the grabber's role=status / role=alert watcher when
    # a toast container became visible during an interaction window.
    toast_text: Optional[str] = None
    """For ``toast`` events: the toast's textContent (trimmed, capped
    at 512 chars). Drives ToastEffect.text_pattern via substring
    matching."""
    toast_level: Optional[Literal["info", "success", "warning", "error"]] = None
    """For ``toast`` events: severity inferred from class names +
    role. ``error`` for role=alert / .toast-error / .error variants;
    ``success`` for .toast-success / .success classes; ``warning``
    for .warning / .warn; default ``info``. The annotator stamps this
    onto ToastEffect.level which controls runner failure on error."""
    toast_selector: Optional[str] = None
    """For ``toast`` events: a CSS selector the runner can re-resolve
    to assert toast_visible. Prefers data-testid > id > role+class."""
    toast_action_buttons: Optional[list[dict[str, Any]]] = None
    """For ``toast`` events: interactive controls inside the toast
    (Undo / Retry / View / Dismiss). Each entry: {label, test_id,
    action_kind}. Drives ToastEffect.dependent_actions."""

    # WI-41: keyboard shortcut payload. Populated on kind='key' events
    # whose raw_event_kind='shortcut' (the grabber sets this for
    # modifier+key chords and standalone F-keys / '/' / '?'). The list
    # is the chord's modifiers in Playwright key-name form (Control /
    # Meta / Shift / Alt) for the annotator to feed straight into
    # ShortcutSpec.modifiers. ``value`` carries the non-modifier key.
    shortcut_modifiers: Optional[list[Literal[
        "Control", "Meta", "Shift", "Alt",
    ]]] = None

    # WI-40: hover payload. Populated on kind='hover' events emitted
    # by the grabber's pointerenter listener when a hover revealed a
    # submenu within a short window. Audit + annotator inputs only;
    # the runner consumes the HoverEffect built from these fields.
    submenu_selector: Optional[str] = None
    """For ``hover`` events: CSS selector of the submenu container
    that became visible after the hover. None means the grabber
    couldn't pinpoint a single submenu (multiple appeared, or none
    appeared within the window). The annotator may still emit a
    HoverEffect with opens_submenu_selector=None and let the runner
    fall back to a small fixed dwell."""
    dwell_ms: Optional[int] = None
    """For ``hover`` events: how long the operator hovered before the
    submenu appeared (ms). Audit-only -- the runner waits for the
    visibility signal, not for this duration."""

    # WI-39: rich-text editor burst payload. Populated by the grabber's
    # contenteditable input listener on raw_event_kind="rich_text_input"
    # input_change events. ``rich_text_html`` is the editor root's
    # innerHTML at the END of the burst; ``rich_text_framework`` is the
    # detected editor framework (tinymce / quill / lexical / unknown).
    # Plain ``value`` carries the textContent fallback so consumers that
    # don't care about HTML can still bind a SkillParam.
    rich_text_html: Optional[str] = None
    """For rich-text bursts: editor root's innerHTML at end-of-burst.
    Capped at 64KB by the grabber to bound payload size; the runner
    only uses this for the recorded_html audit field on RichTextSpec.
    None for non-contenteditable input events."""
    rich_text_framework: Optional[Literal[
        "tinymce", "quill", "lexical", "prosemirror", "slate", "draft",
        "unknown",
    ]] = None
    """For rich-text bursts: detected editor framework. The grabber
    inspects window.tinymce / window.Quill / data-lexical-editor /
    class names at burst time. None for non-contenteditable input
    events; ``unknown`` for raw contenteditable without a detected
    framework."""
