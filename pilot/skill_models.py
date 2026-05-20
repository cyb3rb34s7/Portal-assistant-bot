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
    "navigate",
    "click",
    "change",         # form input value set (debounced typing, select, etc.)
    "submit",
    "key",            # standalone key press (Enter, Escape) not tied to a change
    "upload",         # file input selection
    "wait",           # explicit wait / sleep
    "assert",         # post-condition assertion
    "set_selection",  # multi-select reconciliation (replace/add/remove items in a picker)
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


class Skill(BaseModel):
    """A learned, parameterized, replayable skill."""

    name: str
    description: str = ""
    portal: Optional[str] = None                 # e.g. "sample_portal"
    tags: list[str] = Field(default_factory=list)
    version: int = 1
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
