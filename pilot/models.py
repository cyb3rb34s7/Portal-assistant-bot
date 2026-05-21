"""Typed models shared across the pilot package.

These models define the contract between the runner, the adapters, and the
audit logger. They deliberately mirror the shapes described in the
architecture doc so the POC code can be promoted without rewrite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Standard return type for every adapter method.

    Rule: every adapter method returns exactly this shape. The runner and
    audit logger depend on it. Do not return anything else.
    """

    success: bool
    action_taken: str
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    screenshot_path: Optional[str] = None
    confidence: Optional[Literal["high", "medium", "low"]] = None
    # Set by skill_runner when a step's locator was self-healed (L3).
    # The dict carries: original_summary, new_summary, confidence,
    # reason, post_condition_passed, new_fingerprint (full).
    # Consumed by orchestrators to emit StepHealed + persist alternates.
    healed: Optional[dict[str, Any]] = None
    # Structured failure tag so the orchestrator can route the step into
    # a specific recovery path instead of parsing error strings. Common
    # values: "ambiguous_target" (locator matched > 1 element where the
    # recording matched 1), "row_not_found" (nothing matches),
    # "option_not_available" (select option no longer valid),
    # "post_condition_failed" (heal ran but page didn't change).
    error_kind: Optional[str] = None
    # Free-form structured detail keyed by error_kind. For
    # ambiguous_target this carries {candidates: [{label, test_id, ...}]}
    # which the UI's pause modal renders as a row picker.
    error_details: Optional[dict[str, Any]] = None


class Task(BaseModel):
    """A single task to execute in the runner.

    The `action` field names the adapter method to call. The `params` dict
    is spread into the method's keyword arguments.
    """

    task_id: str
    adapter: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    requires_approval_gate: bool = False
    expected_result: str = ""
    status: Literal[
        "pending", "running", "done", "failed", "skipped", "rejected"
    ] = "pending"


class TaskList(BaseModel):
    """A named, ordered list of tasks loaded from a JSON file."""

    name: str
    description: str = ""
    tasks: list[Task]


class AuditEvent(BaseModel):
    """A single entry written to the session's audit log."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    session_id: str
    task_id: Optional[str] = None
    kind: str  # "task_start" / "task_end" / "gate" / "error" / "info"
    message: str
    data: Optional[dict[str, Any]] = None


class LocatorProbeResult(BaseModel):
    """WI-23: structured outcome of probing a Playwright locator.

    Replaces the legacy ``_first_visible`` boolean helper that caught
    every exception and degraded to ``count() > 0``. That mask hid hidden
    / strict-mode / detached / timeout failures behind a "looks fine"
    signal, so the runner would click on elements that weren't actually
    interactable.

    The runner branches on ``state`` instead of a truthy bool:
      - ``visible_unique``: exactly one visible match -- proceed.
      - ``zero_matches``: locator resolved to nothing -- caller falls
                          through to the next level.
      - ``multiple_matches``: more than one match visible -- WI-22
                              ambiguity detection runs against the
                              candidate list.
      - ``hidden``: matches found but none is visible (display:none,
                    visibility:hidden, opacity:0, offscreen).
      - ``detached``: target element exists in the count but the
                      handle is no longer attached -- typically a
                      re-render race.
      - ``strict_mode_error``: Playwright strict-mode rejected the
                               selector (resolved to multiple).
      - ``timeout``: the probe itself timed out -- the page is still
                     mid-action; caller should wait + retry, not click.
    """

    state: Literal[
        "visible_unique",
        "zero_matches",
        "multiple_matches",
        "hidden",
        "detached",
        "strict_mode_error",
        "timeout",
    ]
    count: int = 0
    """Number of elements the locator resolved to. 0 for zero_matches /
    timeout; >=1 otherwise."""
    last_error: Optional[str] = None
    """Short string describing the underlying Playwright error when
    ``state`` is hidden / detached / strict_mode_error / timeout. None
    on success states."""


class Diagnostic(BaseModel):
    """WI-06: structured diagnostic emitted when a previously-silent
    failure site (bad payload, screenshot fail, watcher install fail,
    persistence fail, ambiguity scan crash, set_selection swallow) is
    converted from ``except: pass`` into a surfaced event.

    The replay event stream (orchestrator) surfaces these at
    ``level="warn"`` for non-fatal and ``level="error"`` for fatal.
    The Replay UI's PausedModal / LogPane renders them.

    Each diagnostic carries:
      - ``code``: short stable identifier (snake_case). Used by tests +
        UI to route to the right rendering. Examples:
        ``teach.bad_payload_json``, ``teach.invalid_fingerprint``,
        ``teach.screenshot_failed``, ``teach.snapshot_drop``,
        ``runner.watcher_install_failed``,
        ``runner.set_selection_search_failed``,
        ``runner.ambiguity_scan_failed``,
        ``executor.hint_persist_failed``,
        ``executor.alternate_persist_failed``.
      - ``context``: structured details. Whatever the call site can
        capture without leaking secrets.
      - ``recoverable``: True if the calling code is continuing (the
        operator can ignore); False if the calling code is also
        bubbling up a failure. Drives the UI's severity badge.
    """

    code: str
    """Stable snake_case identifier for the diagnostic site. UI routes
    on this; tests assert on it."""
    context: dict[str, Any] = Field(default_factory=dict)
    """Structured details: file paths, exception class + message,
    relevant identifiers (step_index, session_id, payload size).
    Should not contain secrets / passwords."""
    recoverable: bool = True
    """True: the calling code continued past the failure; the user
    can ignore. False: the calling code also surfaced a hard failure;
    this diagnostic explains WHY for the audit trail.

    Defaults to True because the predominant audit pattern is
    'we swallowed this exception and continued' -- WI-06 surfaces it
    rather than changing the recoverability."""
    level: Literal["warn", "error", "debug"] = "warn"
    """warn = non-fatal, surfaces in the log pane.
    error = fatal, also routed through StepFailed event when applicable.
    debug = noisy, off by default in the UI."""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
