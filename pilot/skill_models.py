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

from pydantic import BaseModel, Field


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

    # Frame / shadow path (empty = top document)
    frame_path: list[str] = Field(default_factory=list)
    in_shadow_root: bool = False

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
    options_snapshot: Optional[list[dict[str, str]]] = None
    """For selects, listboxes, comboboxes: every option's value + label
    + selected flag at the moment of recording. The annotator uses
    this to emit declared aliases (WI-25) so replay doesn't fuzzy-
    match wrong locale/status options. Each entry: ``{value, label,
    selected?}``."""
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
    ]
    selector: Optional[str] = None
    """CSS selector. Required for visible / hidden / count_* / attr_equals."""
    n: Optional[int] = None
    """For count_* kinds: the expected count."""
    text: Optional[str] = None
    """For text_contains: substring expected somewhere on the page or
    inside ``selector`` if provided. For url_contains: substring expected
    in the current URL."""
    attr: Optional[str] = None
    """For attr_equals: attribute name."""
    timeout_ms: int = 4000


class NetworkExpectation(BaseModel):
    """One network call the runner should wait for after the action.

    URL match is a case-insensitive substring (NOT regex) so portal
    URLs with query params are still easy to match. ``optional`` means
    "wait if it appears within max_ms, but don't fail if it doesn't" --
    used for stale GET requests that may be cached at replay.
    """

    url_pattern: str
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] = "GET"
    status: Optional[int] = None
    """Expected status code. If set, the call only counts as 'completed
    correctly' when this status is observed. None = any 2xx."""
    max_ms: int = 5000
    optional: bool = False


class DomExpectation(BaseModel):
    """One DOM condition the runner should wait for after the action."""

    kind: Literal[
        "visible",
        "hidden",
        "options_changed",  # for cascading dropdowns: the option list mutated
        "count_changed",
    ]
    selector: str
    stable_ms: int = 250
    """How long the condition must hold before we proceed. Prevents
    flicker -- a dropdown that briefly empties then refills shouldn't
    falsely satisfy options_changed."""
    timeout_ms: int = 5000


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


class ModalEffect(BaseModel):
    """An action that opens or closes a modal/dialog."""

    kind: Literal["open", "close"]
    dialog_test_id: Optional[str] = None
    dialog_selector: Optional[str] = None
    expected_visibility: Optional[bool] = None
    """After the action: True = dialog visible, False = dialog hidden."""


class ToastEffect(BaseModel):
    """A transient toast/snackbar that appeared after the action.

    Used for save-confirm toasts, conflict-resolution toasts, undo
    toasts. May carry an action button the workflow can click."""

    message_matcher: Optional[str] = None
    """Substring or regex the toast text must match."""
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
    confidence: Optional[float] = None
    """Annotator's confidence in this provenance (0.0-1.0). LLM-derived
    provenance carries lower confidence than deterministic; operator
    review can boost to 1.0."""


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
    """Name of the list parameter. Resolved to ``list[str]`` at replay."""

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

    set_selection: Optional[SetSelectionSpec] = None
    """Spec for action='set_selection' steps. Carries the picker
    open/close fingerprints, the search/checkbox templates, and the
    reconciliation mode."""

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

    # Debug / context
    captured_at: Optional[datetime] = None
    screenshot_path: Optional[str] = None
    auto_post_observed: Optional[dict[str, Any]] = None   # auto-observed DOM diff


class SkillParam(BaseModel):
    """Declared parameter of a skill."""

    name: str
    type: Literal[
        "string",
        "number",
        "date",
        "file_path",
        "string_list",  # multi-value (categories, tags)
    ] = "string"
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
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    base_url: Optional[str] = None

    params: list[SkillParam] = Field(default_factory=list)
    steps: list[SkillStep]

    # Provenance
    source_session_id: Optional[str] = None

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


def _build_replay_selectors(fp: Optional[ElementFingerprint]) -> list[list[str]]:
    """Emit Puppeteer Replay selector-array-of-arrays from a fingerprint."""
    if fp is None:
        return []
    out: list[list[str]] = []
    if fp.test_id:
        out.append([f"[data-testid='{fp.test_id}']"])
    if fp.element_id:
        out.append([f"#{fp.element_id}"])
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
        "click",
        "input_change",
        "submit",
        "file_selected",
        "navigate",
        "key",
    ]
    fingerprint: Optional[ElementFingerprint] = None
    value: Optional[str] = None
    url: Optional[str] = None
    file_name: Optional[str] = None
    page_url: str = ""
    screenshot_path: Optional[str] = None
    dom_diff: Optional[dict[str, Any]] = None

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
