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

    # Debug / context
    captured_at: Optional[datetime] = None
    screenshot_path: Optional[str] = None
    auto_post_observed: Optional[dict[str, Any]] = None   # auto-observed DOM diff


class SkillParam(BaseModel):
    """Declared parameter of a skill."""

    name: str
    type: Literal["string", "number", "date", "file_path"] = "string"
    description: str = ""
    example: Optional[str] = None
    required: bool = True


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
