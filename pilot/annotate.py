"""Annotation — convert a recorded trace into a reusable Skill.

Loads sessions/<id>/trace.jsonl, filters noise, auto-derives defaults,
and either:

  - --auto       : apply heuristics and save without prompting
  - (interactive): walk through each step with prompts for label,
                   parameter binding, gate flag, noise skip

Output: skills/<name>.json conforming to skill_models.Skill.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .param_codecs import infer_param_type_and_codec
from .skill_models import (
    ActionType,
    AmbiguityPolicy,
    AuthPrecondition,
    AutocompleteSpec,
    DatePickerSpec,
    DependencyChain,
    DomExpectation,
    DragDropSpec,
    ElementFingerprint,
    ExpectedSignals,
    FileMetadata,
    FileSpec,
    FillSubmitSpec,
    ModalCloseAction,
    ModalEffect,
    NavigationEffect,
    NetworkExpectation,
    OptionSnapshot,
    ParamBinding,
    ParamConstraints,
    PageContext,
    PopupEffect,
    RichTextSpec,
    ScrollUntilSpec,
    ShortcutSpec,
    SelectOptionSpec,
    SemanticCluster,
    SetSelectionSpec,
    Skill,
    SkillParam,
    SkillStep,
    SliderSpec,
    StepEffect,
    StepProvenance,
    ToggleStateSpec,
    TraceEvent,
    ValueTransition,
)


GATE_KEYWORDS = re.compile(
    r"\b(delete|remove|publish|submit|send|finalize|approve|register)\b", re.I
)


def load_trace(session_dir: Path) -> list[TraceEvent]:
    path = session_dir / "trace.jsonl"
    events: list[TraceEvent] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(TraceEvent.model_validate_json(line))
        except Exception:
            continue
    return events


def load_meta(session_dir: Path) -> dict:
    p = session_dir / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---- WI-02: causality + identity foundation -----------------------------


def _assign_synthetic_ids(events: list[TraceEvent]) -> list[TraceEvent]:
    """Backfill identity for legacy traces recorded before the
    grabber gained WI-02 event_id/interaction_id support.

    Synthetic IDs are deterministic from file order so a re-annotation
    of the same trace yields the same IDs (good for incremental
    workflows). Events that already have an ``event_id`` from the
    grabber keep theirs untouched.

    Two independent counters: ``id_seq`` for synth-NNNNNN ids,
    ``order_seq`` for the per-event sequence number. These are
    independent so a sequence backfill doesn't perturb id numbering.

    This is a migration helper; new recordings will have IDs in place
    and this function is effectively a no-op for them.
    """
    id_seq = 1
    order_seq = 1
    for ev in events:
        if not ev.event_id:
            ev.event_id = f"synth-{id_seq:06d}"
            id_seq += 1
        if ev.sequence is None:
            ev.sequence = order_seq
        order_seq += 1
    return events


def build_causality_graph(events: list[TraceEvent]) -> dict[str, Any]:
    """Construct the causality graph from a list of TraceEvents.

    Returns:
      {
        "by_id": {event_id: TraceEvent, ...},
        "by_interaction": {interaction_id: [event_id, ...], ...},
        "children_of": {event_id: [child_event_id, ...], ...},
        "user_actions": [event_id, ...]  # events with no caused_by
      }

    Downstream WIs (WI-08 click+navigation collapsing, WI-12 semantic
    clustering) consume this graph to fold consequence events into
    their causing user-action steps without re-deriving causality from
    adjacency.
    """
    by_id: dict[str, TraceEvent] = {}
    by_interaction: dict[str, list[str]] = {}
    children_of: dict[str, list[str]] = {}
    user_actions: list[str] = []
    for ev in events:
        if not ev.event_id:
            continue
        by_id[ev.event_id] = ev
        if ev.interaction_id:
            by_interaction.setdefault(ev.interaction_id, []).append(ev.event_id)
        if ev.caused_by:
            children_of.setdefault(ev.caused_by, []).append(ev.event_id)
        else:
            user_actions.append(ev.event_id)
    return {
        "by_id": by_id,
        "by_interaction": by_interaction,
        "children_of": children_of,
        "user_actions": user_actions,
    }


# ---- Noise filter ---------------------------------------------------------


def filter_events(
    events: list[TraceEvent],
    causality: Optional[dict[str, Any]] = None,
) -> list[TraceEvent]:
    """Drop events that don't belong in a replayable skill.

    F-05: ``causality`` is the graph produced by
    ``build_causality_graph``. It's passed in so future WIs (WI-06,
    WI-13, WI-14) can replace the legacy heuristics below with
    causality-aware checks (e.g. drop a same-URL navigate only when
    its ``caused_by`` is also a navigate to the same URL). Today's
    body does not consume it -- this is wiring only -- but the data
    flow is now correct.
    """
    _ = causality  # reserved -- consumed by WI-06/13/14
    out: list[TraceEvent] = []
    last_nav_url: Optional[str] = None
    prev: Optional[TraceEvent] = None
    for ev in events:
        # Dedupe consecutive navigations to the same URL
        if ev.kind == "navigate":
            if ev.url and ev.url == last_nav_url:
                continue
            last_nav_url = ev.url
            # Keep only the first navigation (the teach session implies
            # we start from a given page). Extra navigations inside the
            # flow stay, but same-URL duplicates are dropped above.

        # WI-13: stop dropping empty input_change events.
        # The legacy heuristic ("drop input_change with empty value
        # immediately after an empty same-target state") silently lost
        # operator clear-and-save workflows. Empty value with a
        # non-empty value_before is a deliberate CLEAR; the annotator
        # downstream marks it clear_intent=True and the runner fills
        # empty string + asserts empty post-action.

        # WI-14: drop ONLY provably-duplicate transport noise (same
        # event_id from a glitch). The legacy <200ms same-target click
        # drop was wrong: a double-click for grid edit, an accordion
        # double-toggle, or a counter-increment all looked like
        # "duplicate clicks" but had distinct semantics. We now keep
        # repeated clicks and rely on the annotator's gesture
        # classification (single / double / repeat / toggle) to pick
        # the right runner action.
        if (
            ev.kind == "click"
            and prev
            and prev.kind == "click"
            and prev.event_id
            and ev.event_id
            and prev.event_id == ev.event_id
        ):
            # Same event_id => transport replay. Drop.
            continue

        out.append(ev)
        prev = ev
    return out


def _same_target(a, b) -> bool:
    if a.test_id and b.test_id:
        return a.test_id == b.test_id
    if a.element_id and b.element_id:
        return a.element_id == b.element_id
    if a.xpath and b.xpath:
        return a.xpath == b.xpath
    return False


# ---- Auto-label heuristics ------------------------------------------------


def auto_label(ev: TraceEvent) -> str:
    fp = ev.fingerprint
    if ev.kind == "navigate":
        return "navigate"
    if not fp:
        return ev.kind
    base = (
        fp.test_id
        or fp.accessible_name
        or fp.aria_label
        or fp.text
        or fp.element_id
        or fp.name
        or fp.tag
        or "element"
    )
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower()
    if not base:
        base = fp.tag or "element"
    if ev.kind == "click":
        return f"click_{base}"[:60]
    if ev.kind == "input_change":
        return f"fill_{base}"[:60]
    if ev.kind == "file_selected":
        return f"upload_{base}"[:60]
    if ev.kind == "submit":
        return f"submit_{base}"[:60]
    if ev.kind == "key":
        return f"key_{(ev.value or 'enter').lower()}_{base}"[:60]
    return base


def action_for_kind(kind: str) -> ActionType:
    return {
        "click": "click",
        "input_change": "change",
        "file_selected": "upload",
        "submit": "submit",
        "navigate": "navigate",
        "key": "key",
        # WI-30: drag/drop events. ``drop`` is the cluster's primary
        # target so this mapping ensures the per-event loop emits the
        # right action; the dragstart/dragover are folded into the
        # cluster's raw_event_ids and never produce a step.
        "drop": "drag_drop",
        "dragstart": "drag_drop",
        "dragover": "drag_drop",
    }.get(kind, "click")  # type: ignore


def infer_param_binding(ev: TraceEvent, label: str) -> Optional[ParamBinding]:
    if ev.kind not in ("input_change", "file_selected"):
        return None
    if not ev.value and not ev.file_name:
        return None
    # Name the param after the field's stable attribute
    fp = ev.fingerprint
    name = None
    if fp:
        name = fp.name or fp.test_id or fp.element_id or fp.accessible_name
    if not name:
        name = label.replace("fill_", "").replace("upload_", "") or "value"
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
    if not name:
        name = "value"
    if ev.kind == "file_selected":
        return ParamBinding(name=name, type="file_path", mode="whole")
    return ParamBinding(name=name, type="string", mode="whole")


def infer_gate(label: str, ev: TraceEvent) -> bool:
    if GATE_KEYWORDS.search(label or ""):
        return True
    if ev.fingerprint and GATE_KEYWORDS.search(ev.fingerprint.text or ""):
        return True
    return False


# ---- WI-08: click + caused navigate collapsing -----------------------------


_SPA_NAV_SOURCES: tuple[str, ...] = (
    "history.pushState",
    "history.replaceState",
    "popstate",
    "hashchange",
)


def _nav_kind_from_source(source: Optional[str]) -> str:
    """Map the grabber's navigate raw_event_kind / source to the
    NavigationEffect.kind Literal. Defaults to ``spa_route`` when the
    source is one of the History API entries; ``full_document`` for
    initial_load + anything we don't recognize."""
    s = (source or "").lower()
    if "pushstate" in s or "popstate" in s:
        return "spa_route"
    if "replacestate" in s:
        return "history_replace"
    if "hashchange" in s:
        return "hash"
    if s == "manual":
        return "manual"
    return "full_document"


def _index_caused_navigates(
    events: list[TraceEvent],
    causality: dict,
) -> dict[str, TraceEvent]:
    """Return a mapping from a user-action event_id to the latest
    navigate event causally attributed to it.

    The grabber attaches ``caused_by`` to each navigate event when the
    History API call fired inside a user handler. Multiple navigates
    can chain off one click (router redirect chain), in which case we
    pick the LAST one (the URL the user ended up on)."""
    out: dict[str, TraceEvent] = {}
    for ev in events:
        if ev.kind != "navigate":
            continue
        if not ev.caused_by:
            continue
        out[ev.caused_by] = ev  # last write wins (router chain)
    return out


def _index_scroll_until_steps(
    events: list[TraceEvent],
    causality: dict[str, Any],
) -> dict[str, ScrollUntilSpec]:
    """WI-37: detect 'operator scrolled to find a row' patterns.

    Walks the trace looking for visibility_change events with
    ``scroller_selector`` set (emitted by the grabber's scroll
    observer during a user interaction). When a subsequent click's
    ancestor chain or fingerprint indicates the click target lived
    INSIDE the scrolled container, we emit a ScrollUntilSpec keyed by
    the click's event_id. The build_skill per-event loop reads this
    map and PREPENDS a synthetic scroll_until step before the click.

    Acceptance (from the plan):
      A recording that scrolled past 50 rows to click row 75 can
      replay against a list that has row 75 at any current scroll
      position.
    """
    out: dict[str, ScrollUntilSpec] = {}
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    # Bucket scroll events by interaction_id so we can identify
    # "this click happened after operator scrolled within the SAME
    # interaction window". Each bucket carries the scroller selector
    # + direction.
    by_interaction: dict[str, list[TraceEvent]] = {}
    for ev in events:
        if ev.kind != "visibility_change":
            continue
        if not ev.scroller_selector:
            continue
        iid = ev.interaction_id or ev.caused_by
        if not iid:
            continue
        by_interaction.setdefault(iid, []).append(ev)

    if not by_interaction:
        return out

    # For each user click, look for scroll events that immediately
    # preceded it whose scroller selector matches an ancestor of the
    # click target.
    for ev in events:
        if ev.kind != "click":
            continue
        if ev.event_id is None or ev.fingerprint is None:
            continue
        # Find scroll events with this interaction OR with a prior
        # interaction whose sequence number is just before this click.
        candidates: list[TraceEvent] = []
        my_iid = ev.interaction_id or ev.event_id
        candidates.extend(by_interaction.get(my_iid, []))
        # Also accept scrolls in the prior interaction window (operator
        # scrolled, then click landed as a fresh interaction).
        # Conservative: only the most recent scrolls within ~the last
        # 30 sequence steps (the operator likely did them just before).
        my_seq = ev.sequence or 0
        for iid, lst in by_interaction.items():
            if iid == my_iid:
                continue
            for sev in lst:
                ssq = sev.sequence or 0
                if 0 <= my_seq - ssq <= 30:
                    candidates.append(sev)
        if not candidates:
            continue
        # Pick the most recent scroll whose selector lives in the click
        # target's ancestor chain (or whose selector matches a known
        # ancestor by testId).
        chain = getattr(ev.fingerprint, "ancestor_chain", None) or []
        chain_test_ids = {
            anc.get("testId")
            for anc in chain
            if isinstance(anc, dict) and anc.get("testId")
        }
        matched_scroll: Optional[TraceEvent] = None
        for sev in candidates:
            sel_testid = _extract_testid_from_selector(
                sev.scroller_selector or ""
            )
            if sel_testid and sel_testid in chain_test_ids:
                matched_scroll = sev
                # keep iterating to pick the latest one
        if matched_scroll is None:
            continue
        # Build the spec: scroller fingerprint approximated from the
        # selector's testid (the click's chain confirms it exists in
        # the recording). target_identity uses the click target's
        # strongest identifying attribute.
        sel_testid = _extract_testid_from_selector(
            matched_scroll.scroller_selector or ""
        )
        scroller_fp = ElementFingerprint(test_id=sel_testid)
        target_identity: dict[str, Any] = {}
        if ev.fingerprint.test_id:
            target_identity["test_id"] = ev.fingerprint.test_id
        elif ev.fingerprint.element_id:
            target_identity["element_id"] = ev.fingerprint.element_id
        if not target_identity:
            continue
        direction = (
            matched_scroll.scroll_direction or "down"
        )
        out[ev.event_id] = ScrollUntilSpec(
            scroller_fp=scroller_fp,
            target_identity=target_identity,
            max_scrolls=50,
            scroll_direction=direction,  # type: ignore[arg-type]
        )
    return out


def _index_modal_effects(
    events: list[TraceEvent],
    causality: dict[str, Any],
) -> tuple[dict[str, ModalEffect], dict[str, str]]:
    """WI-34: build a {causing_event_id -> ModalEffect} map plus a
    {dialog_selector -> opening_event_id} index for scoping in-dialog
    interactions.

    Walks the event list looking for ``kind="modal"`` observations
    (emitted by the grabber's dialog watcher). Each open observation
    that's attributed to a user interaction (caused_by != null) sets
    ``opens_on_action=True`` on the causing event's effect. A close
    observation attributed to the SAME interaction or the next user
    interaction sets ``closes_on_action=True`` on the appropriate
    step.

    Close mechanisms:
      - The opening event is the click that triggered the dialog mount.
      - A close attributed to a SUBSEQUENT user interaction's
        event_id means that interaction CLOSED the dialog. If the
        interaction is a click inside the dialog -> kind="click" with
        target_fp=the click's fingerprint. If it's a key event with
        value=="Escape" -> kind="escape". If it's a click whose
        fingerprint resolves outside the dialog (backdrop) -> kind=
        "backdrop". Conservative default when classification fails:
        kind="click" with the recorded fingerprint.

    Returns:
      - effects_by_cause: causing_event_id -> ModalEffect with
        opens_on_action / closes_on_action / close_actions populated.
      - dialog_scope_by_event: event_id -> dialog_selector for any
        user event that occurred while a dialog was open (so the
        runner / step builder can scope its locator inside the
        dialog).
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    effects_by_cause: dict[str, ModalEffect] = {}
    dialog_scope_by_event: dict[str, str] = {}
    # Active dialog stack -- (dialog_selector, opening_event_id, aria_modal)
    # in DOM open order. Most operations affect the topmost dialog.
    open_dialogs: list[tuple[str, Optional[str], Optional[bool]]] = []

    for ev in events:
        # Track scope of subsequent user events while dialogs are open.
        if ev.kind not in ("modal",) and ev.event_id and open_dialogs:
            # Most-recently-opened dialog wins (scope nests).
            dialog_scope_by_event[ev.event_id] = open_dialogs[-1][0]

        if ev.kind != "modal":
            continue
        sel = ev.dialog_selector or '[role="dialog"]'
        state = ev.dialog_state

        if state == "open":
            opening_id = ev.caused_by or ev.initiator_event_id
            open_dialogs.append((sel, opening_id, ev.dialog_aria_modal))
            if opening_id:
                eff = effects_by_cause.get(opening_id) or ModalEffect()
                eff.opens_on_action = True
                eff.dialog_selector = sel
                # Audit-only legacy fields.
                if eff.dialog_test_id is None:
                    eff.dialog_test_id = _extract_testid_from_selector(sel)
                effects_by_cause[opening_id] = eff
            continue

        if state == "closed":
            # Match the closed dialog to a tracked open one (by selector).
            matched_idx = None
            for i in range(len(open_dialogs) - 1, -1, -1):
                if open_dialogs[i][0] == sel:
                    matched_idx = i
                    break
            if matched_idx is None:
                # Unmatched close (dialog opened before recording started
                # or selector changed). Still record as a close effect on
                # the causing event so the runner verifies dismissal.
                cause_id = ev.caused_by or ev.initiator_event_id
                if cause_id:
                    eff = effects_by_cause.get(cause_id) or ModalEffect()
                    eff.closes_on_action = True
                    eff.dialog_selector = sel
                    effects_by_cause[cause_id] = eff
                continue
            # Pop the matched dialog from the stack.
            opening_tuple = open_dialogs.pop(matched_idx)
            cause_id = ev.caused_by or ev.initiator_event_id
            if not cause_id:
                continue
            cause_ev = by_id.get(cause_id)
            # Decide whether the close belongs on the OPENER's step or
            # on the closing user event's step. Convention: a close that
            # happens DURING the same interaction as the open (same
            # cause_id) folds onto the opener. A close caused by a
            # DIFFERENT user interaction folds onto that interaction.
            if cause_id == opening_tuple[1]:
                eff = effects_by_cause.get(cause_id) or ModalEffect()
                eff.opens_on_action = True
                eff.closes_on_action = True
                eff.dialog_selector = sel
                effects_by_cause[cause_id] = eff
                continue
            # Close belongs to ``cause_id`` (a later user event).
            eff = effects_by_cause.get(cause_id) or ModalEffect()
            eff.closes_on_action = True
            eff.dialog_selector = sel
            # Classify the close mechanism from the causing event.
            close_kind: Literal["click", "escape", "backdrop"] = "click"
            close_target_fp: Optional[ElementFingerprint] = None
            if cause_ev is not None:
                if cause_ev.kind == "key" and (cause_ev.value or "") == "Escape":
                    close_kind = "escape"
                    close_target_fp = None
                elif cause_ev.kind == "click":
                    # Backdrop vs in-dialog click distinction: if the
                    # click target's ancestor chain doesn't carry the
                    # dialog selector / role=dialog, classify as
                    # backdrop. Conservative: when we can't tell, treat
                    # as click so the runner has a fingerprint to use.
                    if _click_inside_dialog(cause_ev, sel):
                        close_kind = "click"
                        close_target_fp = cause_ev.fingerprint
                    else:
                        close_kind = "backdrop"
                        close_target_fp = None
            eff.close_actions.append(
                ModalCloseAction(
                    kind=close_kind,
                    target_fp=close_target_fp,
                )
            )
            effects_by_cause[cause_id] = eff
            continue

    return effects_by_cause, dialog_scope_by_event


def _extract_testid_from_selector(sel: str) -> Optional[str]:
    """Extract a testid from a ``[data-testid="..."]`` selector when
    possible. Returns None for role / index-based selectors."""
    if not sel:
        return None
    m = re.match(r'\[data-testid="([^"]+)"\]', sel)
    if m:
        return m.group(1)
    return None


def _click_inside_dialog(ev: TraceEvent, dialog_selector: str) -> bool:
    """Best-effort check whether a click happened INSIDE the given
    dialog. Walks the click's ancestor_chain looking for an element
    that matches the dialog selector or carries role=dialog.

    Conservative: when the chain is missing or inconclusive, returns
    True (so the runner has a fingerprint to use rather than falling
    back to backdrop). The audit's WI-34 acceptance check pairs this
    with the runner verifying the dialog actually dismisses, so a
    mis-classified close still fails loudly rather than silently
    completing."""
    fp = ev.fingerprint
    if fp is None:
        return True
    chain = getattr(fp, "ancestor_chain", None) or []
    target_testid = _extract_testid_from_selector(dialog_selector)
    for anc in chain:
        if not isinstance(anc, dict):
            continue
        if anc.get("role") == "dialog":
            return True
        if anc.get("tag") == "dialog":
            return True
        if target_testid and anc.get("testId") == target_testid:
            return True
    return False


def _index_validation_errors(
    events: list[TraceEvent],
) -> dict[str, list["StepAssertion"]]:
    """WI-44: build a {causing_event_id -> [StepAssertion(validation_field), ...]}
    map from the grabber's WI-44 validation_invalid readiness events.

    The grabber emits dom_mutation events with
    ``mutation_summary.readiness = {kind='validation_invalid', selector, value}``
    when an element's aria-invalid flipped to 'true' during a user
    interaction window. We translate each into a StepAssertion(kind=
    validation_field) the runner can re-probe at replay to surface
    the same validation failure structurally.

    The annotator stamps these as ASSERTIONS (not expected_signals)
    so they fail the step with error_kind=server_validation when the
    same validation fires again at replay. Operators who EXPECT a
    field to fail validation (negative-path testing) keep the
    assertion; operators who want the field to succeed remove it.
    """
    from .skill_models import StepAssertion as _StepAss
    out: dict[str, list[_StepAss]] = {}
    for ev in events:
        if ev.kind != "dom_mutation":
            continue
        if not ev.caused_by:
            continue
        readiness = (ev.mutation_summary or {}).get("readiness")
        if not isinstance(readiness, dict):
            continue
        rk = readiness.get("kind")
        sel = readiness.get("selector")
        if rk != "validation_invalid" or not sel:
            continue
        # Derive a validation_field_id from the selector by stripping
        # the common selector prefixes (the runner can still resolve
        # via the full selector).
        field_id = sel
        if sel.startswith('[data-testid="') and sel.endswith('"]'):
            field_id = sel[len('[data-testid="'):-len('"]')]
        elif sel.startswith("#"):
            field_id = sel[1:]
        ass = _StepAss(
            kind="validation_field",
            selector=sel,
            validation_field_id=field_id,
            validation_level="error",
            # message_pattern stays None so any non-empty validation
            # message satisfies the assertion. Operators can tighten
            # the pattern post-annotate when they want exact match.
        )
        out.setdefault(ev.caused_by, []).append(ass)
    return out


def _index_readiness_signals(
    events: list[TraceEvent],
) -> dict[str, list[DomExpectation]]:
    """WI-10: build a {causing_event_id -> [DomExpectation, ...]} map
    from the grabber's observed-readiness dom_mutation events.

    The grabber's readiness watcher emits dom_mutation events with
    ``mutation_summary.readiness = {kind, selector, value}`` whenever
    aria-busy clears or a disabled attr is removed during a user
    interaction. We translate each into a DomExpectation the runner
    can wait on.

    Multiple readiness events can attribute to one causing event
    (e.g. Save button: disabled-until-enabled AND aria-busy clears).
    The annotator merges them into the step's expected_signals.dom.
    """
    out: dict[str, list[DomExpectation]] = {}
    for ev in events:
        if ev.kind != "dom_mutation":
            continue
        if not ev.caused_by:
            continue
        readiness = (ev.mutation_summary or {}).get("readiness")
        if not isinstance(readiness, dict):
            continue
        rk = readiness.get("kind")
        sel = readiness.get("selector")
        if not rk or not sel:
            continue
        # Map grabber-side readiness kind onto DomExpectation.kind.
        # ``aria_busy`` / ``disabled_until_enabled`` /
        # ``field_enabled`` / ``role_progressbar_hidden`` /
        # ``text_transition`` / ``selector_hidden`` are all valid.
        if rk not in (
            "aria_busy", "disabled_until_enabled", "field_enabled",
            "role_progressbar_hidden", "text_transition", "selector_hidden",
        ):
            continue
        de = DomExpectation(
            kind=rk,  # type: ignore[arg-type]
            selector=sel,
            text=readiness.get("text"),
        )
        out.setdefault(ev.caused_by, []).append(de)
    return out


def _derive_url_template(
    nav_url: Optional[str],
    params_seen: list[tuple[str, str]],
    *,
    click_fp: Optional[Any] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Derive a ``url_template`` for a click's navigation effect.

    WI-11 returns ``(url_template, url_template_source)`` so callers
    can stamp the source onto the NavigationEffect for downstream
    audit. Sources are ranked strongest-first:

      1. ``route_param`` -- a recorded param value equals the URL's
         final path segment exactly. The path segment IS the param
         value (e.g. ``/asset/A-9001`` with content_id=A-9001 ->
         ``/asset/{content_id}``).
      2. ``row_key`` -- the click target's ancestor chain confirms the
         value as a row key AND it appears as a clean path segment.
         Provided here for completeness; the route_param check above
         already accepts segment-exact matches, so this only fires when
         path slicing matters (e.g. ``/foo/A-9001/bar`` where the
         segment is mid-URL, not last).
      3. None -- no provenance-grade match. Returns the literal URL
         unchanged so replay verifies against the recorded URL only.

    Boundary rule: segments must be separated by ``/`` or ``?`` or end
    of string. This rejects the substring-luck pattern flagged by the
    audit (``A-90`` inside ``A-9001`` does NOT match a route_param)."""
    if not nav_url:
        return (None, None)
    try:
        # Strip query + hash to look at the path only.
        path_only = nav_url.split("?", 1)[0].split("#", 1)[0]
        # Split into segments preserving leading slash. Each segment is
        # a candidate for an exact param-value match.
        segments = path_only.rstrip("/").split("/")
    except Exception:
        return (None, None)

    # Build the ancestor-chain row-key set from the click's fingerprint
    # so we can distinguish row_key vs operator_input provenance for
    # mid-path segments. Empty when fingerprint is missing or the chain
    # carries no row identity.
    row_key_param_names: set[str] = set()
    if click_fp is not None:
        chain = getattr(click_fp, "ancestor_chain", None) or []
        for name, value in params_seen:
            if _ancestor_value_for_param(chain, value):
                row_key_param_names.add(name)

    # Walk segments looking for an EXACT match against any recorded
    # param value. Multiple matches on different segments are fine
    # (each gets its own placeholder); a single segment matching
    # multiple params is ambiguous and we bail.
    new_segments = list(segments)
    matched_param_name: Optional[str] = None
    substituted = False
    for i, seg in enumerate(new_segments):
        if not seg or len(seg) < 3:
            continue
        seg_hits: list[tuple[str, str]] = [
            (n, v) for n, v in params_seen if v == seg
        ]
        if len(seg_hits) == 1:
            n, _ = seg_hits[0]
            new_segments[i] = "{" + n + "}"
            substituted = True
            matched_param_name = n  # last winner; used for source ranking
        elif len(seg_hits) > 1:
            # Ambiguous segment -- bail, keep literal URL.
            return (nav_url, None)

    if not substituted:
        return (nav_url, None)

    templated = "/".join(new_segments)
    # Reattach query / hash if the original had them.
    rest = nav_url[len(path_only):]
    if rest:
        templated = templated + rest

    # Source: row_key beats route_param when the click's ancestor chain
    # vouches for the bound param value; otherwise the URL path
    # supplied the provenance directly (route_param).
    source = (
        "row_key" if matched_param_name in row_key_param_names
        else "route_param"
    )
    return (templated, source)


# ---- WI-14: click gesture / effect classification --------------------------


_STATE_ATTRS_FOR_TOGGLE: tuple[str, ...] = (
    "aria_expanded", "aria_checked", "aria_pressed", "aria_selected",
    "disabled",
)


def _state_diff(
    before: Optional[dict[str, Any]],
    after: Optional[dict[str, Any]],
) -> dict[str, list[Any]]:
    """Return ``{attr: [before, after]}`` for every attribute whose
    value changed between the two snapshots. Empty dict means no
    state change observed.

    WI-14: this is the ``effect_signature`` stamped onto the click
    step. Cheap diff -- only the keys present in either snapshot are
    considered."""
    if not before and not after:
        return {}
    out: dict[str, list[Any]] = {}
    keys: set[str] = set()
    if before:
        keys.update(before.keys())
    if after:
        keys.update(after.keys())
    for k in keys:
        bv = before.get(k) if before else None
        av = after.get(k) if after else None
        if bv != av:
            out[k] = [bv, av]
    return out


def classify_click_gesture(
    ev: TraceEvent,
    prior_clicks: list[TraceEvent],
) -> Optional[str]:
    """WI-14: derive a click_gesture from the grabber-captured
    click_detail + target_state_before/after.

    Heuristics, strongest-first:
      - ``double``: detail >= 2 (the browser already reports it as a
        double-click on the second click of a fast pair).
      - ``toggle``: aria-expanded / aria-checked / aria-pressed flipped
        between before and after (state attributes diff).
      - ``open`` / ``close``: aria-expanded went from false->true (open)
        or true->false (close). More specific than toggle.
      - ``repeat``: same target as the previous click, less than ~1s
        apart, NEITHER of them double-clicks. Distinguishes from
        accidental jitter (we keep both clicks) by the explicit gesture
        kind so the runner can choose to .click() N times or pick a
        desired state.
      - ``single``: default for everything else.

    Returns None when there's no fingerprint to compare (legacy events)."""
    if ev.kind != "click":
        return None
    if ev.click_detail and ev.click_detail >= 2:
        return "double"
    diff = _state_diff(ev.target_state_before, ev.target_state_after)
    if "aria_expanded" in diff:
        before, after = diff["aria_expanded"]
        if before == "false" and after == "true":
            return "open"
        if before == "true" and after == "false":
            return "close"
        return "toggle"
    # aria_checked / aria_pressed / aria_selected flip => toggle
    for k in ("aria_checked", "aria_pressed", "aria_selected"):
        if k in diff:
            return "toggle"
    # Repeated click on same target without state change.
    if prior_clicks and prior_clicks[-1].fingerprint and ev.fingerprint:
        prev = prior_clicks[-1]
        if (
            _same_target(prev.fingerprint, ev.fingerprint)
            and (ev.ts - prev.ts).total_seconds() < 1.0
        ):
            return "repeat"
    return "single"


# ---- WI-13: value transition + clear detection -----------------------------


_COMMIT_EVENT_KINDS: frozenset[str] = frozenset({
    "submit", "key", "navigate",
    # A click after the empty input is treated as a commit signal -- it
    # could be a save button (the legacy semantics here can't tell save
    # from any other click without the WI-12 cluster detector). The
    # narrower checks (save/publish text match) live in WI-15/19.
    "click",
})


def _has_commit_signal_after(
    events: list[TraceEvent], idx: int, target_fp
) -> bool:
    """WI-13: scan forward from ``events[idx]`` for a 'commit' signal
    that ratifies the empty-value as deliberate clear-intent.

    A commit signal is one of:
      - a submit event,
      - a key=Enter,
      - a navigate (operator left the page),
      - a click anywhere (most commit buttons are clicks; we cannot
        narrow without WI-15's save-button detector).

    Blur isn't a separate event kind; the grabber flushes pending input
    on blur which translates the operator's tab-out into an
    input_change. So a blur is captured implicitly by the empty-value
    event already existing -- if the field would have stayed focused,
    the value wouldn't have flushed.

    Returns True when at least one commit signal is found AFTER the
    empty input on the SAME target's form ancestor or later in the
    trace. Conservative on the same-form check: we only look at later
    events, not just same-form, because the trace is small.
    """
    _ = target_fp  # reserved -- future use for same-form matching
    for j in range(idx + 1, len(events)):
        ev = events[j]
        if ev.kind in _OBSERVED_EVENT_KINDS:
            continue
        if ev.kind in _COMMIT_EVENT_KINDS:
            return True
        # Another input on a DIFFERENT field also counts as commit:
        # the operator left the cleared field's focus, which is the
        # blur that ratifies the clear.
        if ev.kind == "input_change":
            if (
                ev.fingerprint is None
                or target_fp is None
                or not _same_target(target_fp, ev.fingerprint)
            ):
                return True
    return False


def derive_value_transition(
    ev: TraceEvent,
    events: list[TraceEvent],
    idx: int,
) -> Optional[ValueTransition]:
    """Build a ValueTransition for a change step from a TraceEvent and
    its position in the (filtered) event list.

    The transition includes:
      - from_recorded: ev.value_before (None for legacy traces)
      - to_recorded: ev.value or "" (empty is valid)
      - clear_intent: True iff before was non-empty, after is empty,
                      AND a commit signal follows (per WI-13).

    Returns None for non-text events (we don't track transitions on
    clicks / keys / file uploads -- WI-13 scope is text + textarea +
    contenteditable + native inputs that emit input_change).
    """
    if ev.kind != "input_change":
        return None
    before = ev.value_before
    after = ev.value if ev.value is not None else ""
    clear_intent = False
    if before is not None and before != "" and after == "":
        if _has_commit_signal_after(events, idx, ev.fingerprint):
            clear_intent = True
    # Even when not a clear, persist the transition so audit / replay
    # can verify the operator's recorded before-state matches what
    # they'll see at replay. If there's NO before-value (legacy trace)
    # and after is non-empty, the transition isn't informative -- skip.
    if before is None and not clear_intent:
        # Still emit when after is empty -- the runner needs to know
        # to fill('') vs not fill at all.
        if after == "":
            return ValueTransition(
                from_recorded=None, to_recorded="", clear_intent=False
            )
        return None
    return ValueTransition(
        from_recorded=before, to_recorded=after, clear_intent=clear_intent,
    )


# ---- WI-12: semantic clustering pipeline -----------------------------------


# Observed events that contribute to a cluster's audit trail but are not
# themselves user-facing steps. These get folded into the owning cluster's
# raw_event_ids (so the audit log shows "this fill_submit step came from
# input + input + submit + 2 network responses") but never produce
# standalone clusters / steps.
_OBSERVED_EVENT_KINDS: frozenset[str] = frozenset({
    "dom_mutation", "network_request", "network_response",
    "popup", "download", "visibility_change",
    # WI-34: dialog mount/unmount observations. The grabber emits
    # ``modal`` events when a role=dialog appears/disappears; the
    # annotator folds them into the causing user action's
    # effects.modal field rather than producing standalone steps.
    "modal",
    # WI-42: toast / snackbar observations. Folded into the causing
    # step's effects.toast; never emitted as their own step.
    "toast",
})


def _fp_target_id(fp: Optional[ElementFingerprint]) -> Optional[str]:
    """Return a stable identity for a fingerprint -- the strongest
    attribute available (test_id > element_id > name > xpath). Used by
    the WI-15+ detectors to compare event targets without tripping on
    the fingerprint's bbox / nth_of_role differences between adjacent
    captures of the same field."""
    if fp is None:
        return None
    return fp.test_id or fp.element_id or fp.name or fp.xpath or fp.accessible_name


def _detect_fill_submit_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-15: detect a typing burst + submit on the same field.

    Walks the event list looking for contiguous input_change events on
    a single field, immediately followed by ONE of:
      - a key event with value=='Enter' on the same field, or
      - a submit event whose target's ancestor form contains the field, or
      - a click on a submit/search button (recognized by:
          * submit-button text (Search / Submit / Apply / Go / Find), or
          * the click's target being inside a form that contains the
            same input).

    Emits ONE ``fill_submit`` cluster carrying ALL input_change events
    + the trigger event. The primary_target_event_id points at the
    last input_change (which holds the final value) so the
    step-construction pass can lift the binding from it.

    Consumed event ids are added to ``consumed`` so the caller can skip
    them in its single_event fallback loop.
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []

    i = 0
    n = len(events)
    while i < n:
        ev = events[i]
        if (
            ev.event_id is None
            or ev.event_id in consumed
            or ev.kind != "input_change"
            or ev.event_id not in user_actions
        ):
            i += 1
            continue

        # Collect contiguous input_change events on the same target.
        burst_target = _fp_target_id(ev.fingerprint)
        if burst_target is None:
            i += 1
            continue
        burst_events: list[TraceEvent] = [ev]
        j = i + 1
        while j < n:
            ne = events[j]
            if ne.event_id is None:
                j += 1
                continue
            if ne.kind in _OBSERVED_EVENT_KINDS:
                # Observed events between inputs (the page fired requests
                # during typing) don't break the burst -- they're folded
                # into the cluster's audit trail later.
                j += 1
                continue
            if (
                ne.kind == "input_change"
                and _fp_target_id(ne.fingerprint) == burst_target
            ):
                burst_events.append(ne)
                j += 1
                continue
            break

        # Look at the FIRST non-observed event after the burst for a
        # trigger. We don't scan further -- a click on something else
        # ends the burst's submit eligibility (the operator moved on).
        trigger: Optional[TraceEvent] = None
        trigger_kind: Optional[Literal["enter", "button", "form_submit"]] = None
        k = j
        while k < n and events[k].kind in _OBSERVED_EVENT_KINDS:
            k += 1
        if k < n:
            nxt = events[k]
            if (
                nxt.kind == "key"
                and (nxt.value or "").lower() in ("enter", "return")
                and _fp_target_id(nxt.fingerprint) == burst_target
            ):
                trigger = nxt
                trigger_kind = "enter"
            elif nxt.kind == "submit":
                trigger = nxt
                trigger_kind = "form_submit"
            elif nxt.kind == "click":
                # Heuristic submit detection from the recorded button:
                # accept buttons whose accessible name / text matches a
                # short list of submit-y verbs, OR buttons with
                # type='submit' / role='button' inside the form ancestor.
                # The conservative bar is intentional -- click events
                # ARE the most ambiguous trigger; we'd rather under-
                # cluster (leave the click as a separate step) than
                # wrong-cluster (fold a NEXT step into a fill_submit).
                if _looks_like_submit_button(nxt.fingerprint, burst_events[0]):
                    trigger = nxt
                    trigger_kind = "button"

        # A burst with no trigger is just typing -- don't collapse.
        if trigger is None or trigger_kind is None:
            i = j
            continue

        # Build the cluster. raw_event_ids: every input_change + the
        # trigger, in causal order. Fold any observed children of the
        # trigger (the search request) into the audit trail.
        raw_ids: list[str] = [e.event_id for e in burst_events if e.event_id]
        if trigger.event_id:
            raw_ids.append(trigger.event_id)
        # Walk causality.children_of for the trigger to capture network
        # responses caused by the submit (e.g. /api/search?q=...).
        children_of: dict[str, list[str]] = causality.get("children_of") or {}
        if trigger.event_id:
            for cid in children_of.get(trigger.event_id, []):
                ce = by_id.get(cid)
                if ce is not None and ce.kind in _OBSERVED_EVENT_KINDS:
                    if cid not in raw_ids:
                        raw_ids.append(cid)
                    consumed.add(cid)

        # Mark events consumed.
        for e in burst_events:
            if e.event_id:
                consumed.add(e.event_id)
        if trigger.event_id:
            consumed.add(trigger.event_id)

        clusters.append(
            SemanticCluster(
                raw_event_ids=raw_ids,
                cluster_kind="fill_submit",
                primary_target_event_id=burst_events[-1].event_id,
                confidence=1.0,
                alternatives_considered=["single_event"],
            )
        )
        # Continue scanning past the trigger.
        i = k + 1
    return clusters


# Submit-button heuristic: short list of verbs the operator commonly
# uses on the button that commits a form. Kept conservative; the
# alternative was to capture the form's submit handler from the
# grabber, which would be a stronger signal but requires WI-09 / WI-15
# grabber-side hooks we don't ship in this WI. Detector accuracy: the
# regex matches whole words to avoid 'send' inside 'sender' etc.
_SUBMIT_BUTTON_TEXT_RE = re.compile(
    r"\b(search|submit|apply|go|find|filter|lookup|query)\b", re.I
)


def _looks_like_submit_button(
    btn_fp: Optional[ElementFingerprint],
    input_ev: TraceEvent,
) -> bool:
    """Return True iff the clicked element looks like the submit button
    for the burst's form. Two signals:
      (a) the button's accessible_name / text / aria_label matches a
          short submit-verb regex (Search / Submit / Apply / Go / Find),
      (b) the button's input_type is 'submit' (an <input type='submit'>
          or <button type='submit'>; the grabber's WI-03 control_kind
          captures this).
    Either signal is sufficient. Designed to keep WI-15 conservative:
    a recorded click on a random button right after typing is NOT
    folded into the fill_submit cluster -- it stays as its own step.
    """
    _ = input_ev  # reserved for future same-form ancestor walk
    if btn_fp is None:
        return False
    # Signal (b): explicit submit semantics.
    if (btn_fp.input_type or "").lower() == "submit":
        return True
    if btn_fp.control_kind == "button" and (
        btn_fp.role == "button" or btn_fp.tag == "button"
    ):
        # Continue to signal (a) check below.
        pass
    # Signal (a): submit-verb text.
    haystack = " ".join(
        s for s in (
            btn_fp.accessible_name, btn_fp.text, btn_fp.aria_label,
            btn_fp.placeholder,
        ) if s
    )
    if haystack and _SUBMIT_BUTTON_TEXT_RE.search(haystack):
        return True
    return False


def _build_fill_submit_spec(
    cluster: SemanticCluster,
    events: list[TraceEvent],
    causality: dict[str, Any],
    binding: Optional[ParamBinding],
) -> FillSubmitSpec:
    """WI-15: derive a FillSubmitSpec from a fill_submit cluster.

    Walks the cluster's raw_event_ids to find the trigger (Enter / submit
    button click / form submit) and any expected result signal (URL of
    a network_response caused by the trigger). Used at step-construction
    time when the per-event loop's primary_target_event_id maps to a
    fill_submit cluster.
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    trigger_ev: Optional[TraceEvent] = None
    trigger_kind: Literal["enter", "button", "form_submit"] = "enter"
    submit_button_fp: Optional[ElementFingerprint] = None
    expected_signal: Optional[str] = None

    for eid in cluster.raw_event_ids:
        e = by_id.get(eid)
        if e is None:
            continue
        if e.kind == "key" and (e.value or "").lower() in ("enter", "return"):
            trigger_ev = e
            trigger_kind = "enter"
        elif e.kind == "submit":
            trigger_ev = e
            trigger_kind = "form_submit"
        elif e.kind == "click":
            trigger_ev = e
            trigger_kind = "button"
            submit_button_fp = e.fingerprint
        elif e.kind == "network_response" and expected_signal is None:
            # First network_response caused by the trigger is a strong
            # candidate for the expected result signal. Use the URL's
            # path portion as a substring matcher; the runner's
            # NetworkExpectation does case-insensitive substring match.
            url = e.url or ""
            if url:
                try:
                    expected_signal = url.split("?", 1)[0].split("#", 1)[0]
                except Exception:
                    expected_signal = url
        elif e.kind == "network_request" and expected_signal is None:
            url = e.url or ""
            if url:
                try:
                    expected_signal = url.split("?", 1)[0].split("#", 1)[0]
                except Exception:
                    expected_signal = url

    _ = events  # reserved for future same-form ancestor lookup
    _ = trigger_ev
    return FillSubmitSpec(
        submit_trigger=trigger_kind,
        value_param=binding.name if binding else None,
        submit_button_fp=submit_button_fp,
        expected_result_signal=expected_signal,
    )


def _detect_select_autocomplete_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-16: detect a query input + autocomplete network call +
    result-container click. Collapsed to one ``select_autocomplete``
    cluster.

    Pattern: input_change burst on field A -> network_request
    (caused by the input or by a debounce-triggered fetch) ->
    network_response -> click on element X. We collapse when:
      (a) the click target's ancestor chain or fingerprint context
          shows it is INSIDE a result container that appeared after
          the query (an option / listbox-item / autocomplete-row /
          'btn-open-...' inside a results panel), AND
      (b) the click is the next user action after the burst.

    Distinguished from fill_submit by the presence of a click on a
    RESULT (option/row item) rather than a submit button. To keep
    detection conservative and predictable, the WI-16 detector
    requires the click event's fingerprint to satisfy ONE of:
      - input_type / role / control_kind suggests an option / listitem
        (control_kind in {option, treeitem, menuitem}), or
      - test_id contains 'result' / 'row' / 'option', or
      - the click has a caused_by pointing at a network_response, or
      - the click's ancestor_chain contains a listbox / option / menu.

    A separate `query` param vs `selected_item` param is preserved at
    step-construction time -- the detector returns the cluster; the
    spec build at _build_select_autocomplete_spec separates them.
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    user_actions: set[str] = set(causality.get("user_actions") or [])
    children_of: dict[str, list[str]] = causality.get("children_of") or {}
    clusters: list[SemanticCluster] = []

    i = 0
    n = len(events)
    while i < n:
        ev = events[i]
        if (
            ev.event_id is None
            or ev.event_id in consumed
            or ev.kind != "input_change"
            or ev.event_id not in user_actions
        ):
            i += 1
            continue

        burst_target = _fp_target_id(ev.fingerprint)
        if burst_target is None:
            i += 1
            continue
        burst_events: list[TraceEvent] = [ev]
        # Gather contiguous inputs on same field; observed children
        # (network_request) interspersed are fine (autocomplete fires
        # debounced requests during typing).
        j = i + 1
        observed_children: list[TraceEvent] = []
        while j < n:
            ne = events[j]
            if ne.event_id is None:
                j += 1
                continue
            if ne.kind in _OBSERVED_EVENT_KINDS:
                # Autocomplete typically fires network_request /
                # network_response during typing. Track them so we can
                # fold into the cluster's audit trail.
                observed_children.append(ne)
                j += 1
                continue
            if (
                ne.kind == "input_change"
                and _fp_target_id(ne.fingerprint) == burst_target
            ):
                burst_events.append(ne)
                j += 1
                continue
            break

        # Next non-observed event must be a click on a RESULT.
        k = j
        while k < n and events[k].kind in _OBSERVED_EVENT_KINDS:
            observed_children.append(events[k])
            k += 1
        if k >= n:
            i = k
            continue
        click_ev = events[k]
        if click_ev.kind != "click":
            i = j
            continue
        if not _looks_like_autocomplete_result(click_ev):
            i = j
            continue

        # Collect raw event ids: burst inputs + observed children + click.
        raw_ids: list[str] = []
        for e in burst_events:
            if e.event_id:
                raw_ids.append(e.event_id)
        for e in observed_children:
            if e.event_id and e.event_id not in raw_ids:
                raw_ids.append(e.event_id)
        if click_ev.event_id:
            raw_ids.append(click_ev.event_id)
        # Fold any observed children OF the click (e.g. nav fired by the
        # result click -- a search-pick that also navigates).
        if click_ev.event_id:
            for cid in children_of.get(click_ev.event_id, []):
                ce = by_id.get(cid)
                if ce is not None and ce.kind in _OBSERVED_EVENT_KINDS:
                    if cid not in raw_ids:
                        raw_ids.append(cid)
                    consumed.add(cid)

        # Mark consumed.
        for e in burst_events:
            if e.event_id:
                consumed.add(e.event_id)
        for e in observed_children:
            if e.event_id:
                consumed.add(e.event_id)
        if click_ev.event_id:
            consumed.add(click_ev.event_id)

        clusters.append(
            SemanticCluster(
                raw_event_ids=raw_ids,
                cluster_kind="select_autocomplete",
                # primary target is the CLICK -- the spec consumer
                # needs to pick the result identity from its
                # fingerprint, and the step's fingerprint should be
                # the result template so the runner can materialize
                # with a different selected_item.
                primary_target_event_id=click_ev.event_id,
                confidence=1.0,
                alternatives_considered=["fill_submit", "single_event"],
            )
        )
        i = k + 1
    return clusters


# Test-id substrings that strongly suggest 'I'm a row/option/result'.
_AUTOCOMPLETE_RESULT_TEST_ID_RE = re.compile(
    r"(result|row|option|item|suggest|autocomplete|listbox)", re.I
)


def _looks_like_autocomplete_result(ev: TraceEvent) -> bool:
    """WI-16: return True iff the clicked element is inside an
    autocomplete/result container. Looks at:
      - control_kind (option / treeitem / menuitem),
      - role (option / listitem / row / menuitem / treeitem),
      - test_id containing 'result' / 'row' / 'option' / etc.,
      - the click being caused_by a network_response (the
        grabber attributes clicks inside late-rendered containers
        to the request that rendered them).
    """
    if ev.fingerprint is None:
        return False
    fp = ev.fingerprint
    if fp.control_kind in ("option", "treeitem", "menuitem"):
        return True
    if (fp.role or "").lower() in (
        "option", "listitem", "row", "menuitem", "treeitem",
    ):
        return True
    tid = fp.test_id or ""
    if tid and _AUTOCOMPLETE_RESULT_TEST_ID_RE.search(tid):
        return True
    # Element id pattern that suggests result.
    eid = fp.element_id or ""
    if eid and _AUTOCOMPLETE_RESULT_TEST_ID_RE.search(eid):
        return True
    # Last resort: walk the ancestor_chain looking for a listbox /
    # listitem ancestor (the grabber's WI-03 ancestor_chain field).
    for anc in (fp.ancestor_chain or []):
        if not isinstance(anc, dict):
            continue
        anc_role = (anc.get("role") or "").lower()
        anc_test = anc.get("data-testid") or anc.get("test_id") or ""
        if anc_role in ("listbox", "menu", "tree", "grid"):
            return True
        if isinstance(anc_test, str) and _AUTOCOMPLETE_RESULT_TEST_ID_RE.search(
            anc_test
        ):
            return True
    return False


def _build_select_autocomplete_spec(
    cluster: SemanticCluster,
    events: list[TraceEvent],
    causality: dict[str, Any],
    click_binding_name: Optional[str],
) -> tuple[AutocompleteSpec, Optional[str], Optional[ElementFingerprint]]:
    """WI-16: derive an AutocompleteSpec from a select_autocomplete cluster.

    Returns (spec, query_value, query_input_fp). The caller uses
    query_value to seed a separate ``query`` skill param (distinct from
    the click's binding which represents the SELECTED result identity).

    The primary_target is the CLICK event; its fingerprint is the
    result row. We walk the cluster's raw_event_ids to find:
      - the LAST input_change (final query value),
      - the FIRST network_request (the search endpoint URL is the
        signal we wait on at replay),
      - the click event for the option_identity_template (we set
        this to the click's fingerprint -- WI-11 templating will
        derive a {selected_item} placeholder from the click target).
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    last_input: Optional[TraceEvent] = None
    first_request: Optional[TraceEvent] = None
    click_ev: Optional[TraceEvent] = None

    for eid in cluster.raw_event_ids:
        e = by_id.get(eid)
        if e is None:
            continue
        if e.kind == "input_change":
            last_input = e  # later input overrides earlier
        elif e.kind in ("network_request", "network_response") and first_request is None:
            first_request = e
        elif e.kind == "click":
            click_ev = e

    query_value: Optional[str] = (last_input.value if last_input else None)
    # Use the binding name (from the click event's binding) for the
    # selected_item param. If the click event didn't bind to a param
    # the spec falls back to "selected_item".
    selected_item_name = click_binding_name or "selected_item"
    # The query param is conventionally named "query" but if a binding
    # came from the input event the caller can rename it. We declare
    # the standard name here.
    query_name = "query"

    network_expectation: Optional[NetworkExpectation] = None
    if first_request is not None and first_request.url:
        try:
            url_pattern = first_request.url.split("?", 1)[0]
        except Exception:
            url_pattern = first_request.url
        method = (first_request.method or "GET").upper()
        if method not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
            method = "GET"
        network_expectation = NetworkExpectation(
            url_pattern=url_pattern,
            method=method,  # type: ignore[arg-type]
            optional=False,
        )

    # Build option identity template from the click's fingerprint;
    # WI-11 template derivation runs later and will populate
    # templates/template_sources with {selected_item} as the
    # placeholder when the click target's test_id matches the
    # recorded selected_item value.
    option_identity_template = click_ev.fingerprint if click_ev else None

    spec = AutocompleteSpec(
        query_param=query_name,
        selected_item_param=selected_item_name,
        query_input_fp=last_input.fingerprint if last_input else None,
        result_container_fp=None,
        option_identity_template=option_identity_template,
        network_expectation=network_expectation,
    )
    query_input_fp = last_input.fingerprint if last_input else None
    return spec, query_value, query_input_fp


def _detect_select_option_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-17: detect a native <select> change with options_snapshot
    captured at record time -> ONE select_option cluster.

    The grabber's WI-03 control_kind ``select_single`` + the
    options_snapshot on the fingerprint give us everything we need.
    The detector matches input_change events whose fingerprint declares
    ``select_single`` (a single-select native control) AND carries a
    non-empty options_snapshot.

    Cascading parent select (WI-18) is detected separately because it
    needs the child-options-refresh-after-request causal pattern.
    Plain native selects with no causal-network signal land here.
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []

    for ev in events:
        if ev.event_id is None or ev.event_id in consumed:
            continue
        if ev.kind != "input_change" or ev.event_id not in user_actions:
            continue
        fp = ev.fingerprint
        if fp is None:
            continue
        # Only fold when the recording marked it a native single-select
        # AND captured an option snapshot. select_multiple goes through
        # the set_selection detector (WI-19).
        if fp.control_kind != "select_single":
            continue
        if not fp.options_snapshot:
            continue
        # Consume the single event into the cluster.
        consumed.add(ev.event_id)
        clusters.append(
            SemanticCluster(
                raw_event_ids=[ev.event_id],
                cluster_kind="select_option",
                primary_target_event_id=ev.event_id,
                confidence=1.0,
                alternatives_considered=["single_event"],
            )
        )
    return clusters


def _detect_cascading_select(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[tuple[str, str, Optional[TraceEvent]]]:
    """WI-18: detect select_A change -> network call -> select_B
    options change. Returns a list of (parent_event_id, child_event_id,
    option_source_request_event) triples for the annotator to attach
    DependencyChain to the PARENT step.

    Differs from the other detectors in two ways:
      (a) it does NOT create its own SemanticCluster -- the parent and
          child remain as select_option clusters; the dependency_chain
          is overlaid on the parent step.
      (b) it does NOT add events to ``consumed`` -- both selects are
          still emitted as full select_option steps.

    Detection heuristic:
      For each pair of (parent, child) select_option events:
        - parent and child are both native single-selects
          (control_kind=select_single, options_snapshot present),
        - parent occurred BEFORE child in event order,
        - there is a network_request event between them whose
          caused_by chain traces back to the parent,
        - the child's options_snapshot DIFFERS from any prior snapshot
          of the same child element (the options actually changed).
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    user_actions: set[str] = set(causality.get("user_actions") or [])
    children_of: dict[str, list[str]] = causality.get("children_of") or {}

    select_events: list[TraceEvent] = []
    for ev in events:
        if (
            ev.kind == "input_change"
            and ev.event_id in user_actions
            and ev.fingerprint is not None
            and ev.fingerprint.control_kind == "select_single"
            and ev.fingerprint.options_snapshot
        ):
            select_events.append(ev)

    chains: list[tuple[str, str, Optional[TraceEvent]]] = []
    for i, parent in enumerate(select_events):
        # Look for a child select event with a different fingerprint
        # target that appears AFTER this parent in the event stream.
        for child in select_events[i + 1:]:
            if parent.event_id == child.event_id:
                continue
            if _fp_target_id(parent.fingerprint) == _fp_target_id(child.fingerprint):
                continue
            # Find an intermediate network_request caused by parent.
            request_ev: Optional[TraceEvent] = None
            for cid in children_of.get(parent.event_id or "", []):
                ce = by_id.get(cid)
                if ce is None:
                    continue
                if ce.kind in ("network_request", "network_response"):
                    # request must have started before the child event
                    # in trace order.
                    request_ev = ce
                    break
            if request_ev is None:
                continue
            if parent.event_id and child.event_id:
                chains.append(
                    (parent.event_id, child.event_id, request_ev)
                )
            break  # Only chain to the FIRST dependent child per parent
    _ = consumed  # WI-18 doesn't consume events
    return chains


def _build_select_option_spec(
    cluster: SemanticCluster,
    events: list[TraceEvent],
    causality: dict[str, Any],
    ev: TraceEvent,
) -> SelectOptionSpec:
    """WI-17: derive a SelectOptionSpec from the captured event.

    Picks the matched option from the recording's options_snapshot:
      - recorded_value: the option whose value == ev.value
      - recorded_label: the matching option's label

    match_mode defaults to ``value`` (the safe default -- no fuzzy
    fallback). The annotator does NOT auto-populate aliases; aliases
    are operator-declared.
    """
    _ = cluster
    _ = events
    _ = causality
    fp = ev.fingerprint
    recorded_value = ev.value or ""
    recorded_label: Optional[str] = None
    options_snapshot: Optional[list[OptionSnapshot]] = None
    if fp and fp.options_snapshot:
        options_snapshot = list(fp.options_snapshot)
        for opt in options_snapshot:
            if opt.value == recorded_value:
                recorded_label = opt.label
                break
    return SelectOptionSpec(
        recorded_value=recorded_value,
        recorded_label=recorded_label,
        options_snapshot=options_snapshot,
        match_mode="value",
        aliases={},
    )


_MULTISELECT_TOGGLE_RE = re.compile(r"-toggle$")
_MULTISELECT_SEARCH_RE = re.compile(r"-search$")
_MULTISELECT_CHECKBOX_RE = re.compile(r"-checkbox-(.+)$")
_MULTISELECT_ITEM_RE = re.compile(r"-item-(.+)$")
_MULTISELECT_CHIP_RE = re.compile(r"-chip-(?!.*-remove$)(.+)$")


def _multiselect_prefix(fp: Optional[ElementFingerprint]) -> Optional[str]:
    """Return the multiselect testId prefix from a fingerprint, or None.

    Recognizes the MultiSelect.jsx test_id family:
      {prefix}-toggle, -search, -checkbox-X, -item-X, -chip-X, -popover.
    The prefix is the testid with the recognized suffix stripped.
    """
    if fp is None or not fp.test_id:
        return None
    tid = fp.test_id
    for suffix_re in (
        _MULTISELECT_TOGGLE_RE,
        _MULTISELECT_SEARCH_RE,
    ):
        m = suffix_re.search(tid)
        if m:
            return tid[: m.start()]
    # checkbox / item / chip / chip-remove all carry a -{id} segment.
    for suffix_re in (
        _MULTISELECT_CHECKBOX_RE,
        _MULTISELECT_ITEM_RE,
    ):
        m = suffix_re.search(tid)
        if m:
            return tid[: m.start()]
    # chip-X (but not chip-X-remove which is the remove sub-button)
    m = re.search(r"-chip-([^-]+)$", tid)
    if m:
        return tid[: m.start()]
    return None


def _multiselect_role(fp: Optional[ElementFingerprint]) -> Optional[str]:
    """Classify which part of the multiselect a fingerprint is. Returns
    'toggle' / 'search' / 'checkbox' / 'item' / 'chip' / None."""
    if fp is None or not fp.test_id:
        return None
    tid = fp.test_id
    if _MULTISELECT_TOGGLE_RE.search(tid):
        return "toggle"
    if _MULTISELECT_SEARCH_RE.search(tid):
        return "search"
    if _MULTISELECT_CHECKBOX_RE.search(tid):
        return "checkbox"
    if _MULTISELECT_ITEM_RE.search(tid):
        return "item"
    if re.search(r"-chip-[^-]+$", tid):
        return "chip"
    return None


def _multiselect_item_id(fp: Optional[ElementFingerprint]) -> Optional[str]:
    """Extract the item id from a checkbox/item/chip testid."""
    if fp is None or not fp.test_id:
        return None
    tid = fp.test_id
    for rx in (
        _MULTISELECT_CHECKBOX_RE,
        _MULTISELECT_ITEM_RE,
        re.compile(r"-chip-([^-]+)$"),
    ):
        m = rx.search(tid)
        if m:
            return m.group(1)
    return None


def _multiselect_item_label(
    fp: Optional[ElementFingerprint], item_id: str
) -> str:
    """Display label for a multiselect option, used by the runner to
    type into a label-indexed (server-side) search box.

    Priority: accessible_name -> text -> aria_label -> the id itself.
    The checkbox fingerprint's accessible_name is the option's visible
    text (the <span>{name}</span> next to the <input>); when the grabber
    only captured the bare checkbox we fall back through text / aria
    and finally the id (which still lets the runner narrow on a portal
    whose search matches ids, and is harmless when it doesn't because the
    visibility wait gates the click)."""
    if fp is not None:
        for cand in (fp.accessible_name, fp.text, fp.aria_label):
            if cand and cand.strip():
                return cand.strip()
    return item_id


def _detect_set_selection_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-19: detect a custom multi-select widget interaction and
    collapse all toggle/search/check/check/close events into ONE
    ``set_selection`` cluster.

    Pattern (anchored on the toggle-open click):
      click on {prefix}-toggle (open the picker)
      then any combination of:
        - input_change on {prefix}-search,
        - click on {prefix}-checkbox-X (check/uncheck items),
        - click on {prefix}-item-X (alternative click target),
      followed by a final click (commit) -- this can be the toggle
      again (close), an outside click, or just end-of-recording.

    Collapses to ONE cluster carrying ALL the toggle/search/checkbox
    events. primary_target = the LAST checkbox click (the spec
    consumer reads the final selected set from the recording's
    state).
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []

    i = 0
    n = len(events)
    while i < n:
        ev = events[i]
        if (
            ev.event_id is None
            or ev.event_id in consumed
            or ev.event_id not in user_actions
        ):
            i += 1
            continue
        # Anchor on a toggle-open click.
        if ev.kind != "click" or _multiselect_role(ev.fingerprint) != "toggle":
            i += 1
            continue
        prefix = _multiselect_prefix(ev.fingerprint)
        if prefix is None:
            i += 1
            continue

        # Scan forward; consume events whose testid starts with the same
        # prefix until we hit either:
        #   - a toggle on the same prefix (close), OR
        #   - an event NOT in the multiselect family on a DIFFERENT
        #     prefix (commits elsewhere).
        cluster_events: list[TraceEvent] = [ev]
        last_checkbox_event_id: Optional[str] = ev.event_id
        j = i + 1
        while j < n:
            ne = events[j]
            if ne.event_id is None:
                j += 1
                continue
            if ne.kind in _OBSERVED_EVENT_KINDS:
                cluster_events.append(ne)
                j += 1
                continue
            ne_prefix = _multiselect_prefix(ne.fingerprint)
            if ne_prefix == prefix:
                cluster_events.append(ne)
                role = _multiselect_role(ne.fingerprint)
                if role in ("checkbox", "item"):
                    last_checkbox_event_id = ne.event_id
                if role == "toggle" and ne.kind == "click":
                    # Close click -- include and stop.
                    j += 1
                    break
                j += 1
                continue
            # Event NOT in the multiselect prefix. Stop.
            break

        # A cluster of just the toggle-open (no checks) is not a
        # meaningful set_selection -- the operator may have just
        # peeked. Skip.
        if len(cluster_events) <= 1:
            i += 1
            continue

        # A set_selection MUST involve at least one option selection
        # (a checkbox/item click). A bare ``*-toggle`` click followed
        # only by observed events (DOM mutations, network) is NOT a
        # multi-select interaction -- e.g. an accordion disclosure
        # button whose testid happens to end in ``-toggle`` and flips
        # aria-expanded. Without this guard such a click is wrongly
        # claimed here (producing a degenerate set_selection with no
        # checkbox_template_fp + empty known_options) and pre-empts
        # ``_detect_toggle_state_clusters``, which would correctly emit
        # a toggle_state step. Require a real selection event before
        # consuming the cluster; otherwise leave the events for the
        # toggle_state / single-event detectors.
        has_selection = any(
            ce.kind not in _OBSERVED_EVENT_KINDS
            and _multiselect_role(ce.fingerprint) in ("checkbox", "item")
            for ce in cluster_events
        )
        if not has_selection:
            i += 1
            continue

        # Collect raw event ids in order.
        raw_ids: list[str] = []
        for e in cluster_events:
            if e.event_id and e.event_id not in raw_ids:
                raw_ids.append(e.event_id)
                consumed.add(e.event_id)

        clusters.append(
            SemanticCluster(
                raw_event_ids=raw_ids,
                cluster_kind="set_selection",
                primary_target_event_id=(
                    last_checkbox_event_id or ev.event_id
                ),
                confidence=1.0,
                alternatives_considered=["single_event"],
            )
        )
        i = j
    return clusters


def _detect_dependent_multiselect(
    events: list[TraceEvent],
    causality: dict[str, Any],
    set_selection_clusters: list[SemanticCluster],
) -> dict[str, tuple[Optional[TraceEvent], Optional[TraceEvent]]]:
    """WI-20: detect a parent picker (single-select or multi-select)
    whose change drives a child multi-select's option set.

    Returns a mapping from child_cluster.primary_target_event_id to
    (parent_event, option_source_request_event) tuples. The build_skill
    pass overlays SetSelectionSpec.depends_on / parent_picker_fp /
    option_source / hierarchy_path on the child step.

    Detection: for each set_selection cluster, find the most recent
    user-action event before the cluster's first event whose
    fingerprint is a select_single / set_selection-prefix toggle, AND
    a network_request between them whose caused_by chain traces back
    to that parent.
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    children_of: dict[str, list[str]] = causality.get("children_of") or {}
    out: dict[str, tuple[Optional[TraceEvent], Optional[TraceEvent]]] = {}

    # Pre-compute the index of each event in the event list (for
    # 'most recent prior' lookup).
    event_index = {
        ev.event_id: i for i, ev in enumerate(events) if ev.event_id
    }

    for cluster in set_selection_clusters:
        if cluster.cluster_kind != "set_selection":
            continue
        first_raw_id = cluster.raw_event_ids[0] if cluster.raw_event_ids else None
        if not first_raw_id or first_raw_id not in event_index:
            continue
        cluster_start_i = event_index[first_raw_id]

        # Find the most recent user-action select_single change OR
        # a prior set_selection cluster's last event.
        parent_event: Optional[TraceEvent] = None
        for k in range(cluster_start_i - 1, -1, -1):
            cand = events[k]
            if cand.kind != "input_change":
                continue
            if cand.event_id in (cluster.raw_event_ids):
                continue
            if cand.fingerprint is None:
                continue
            if cand.fingerprint.control_kind == "select_single":
                parent_event = cand
                break

        if parent_event is None:
            continue

        # Look for a network_request caused by parent_event whose
        # event index is between the parent and the cluster start.
        request_ev: Optional[TraceEvent] = None
        for cid in children_of.get(parent_event.event_id or "", []):
            ce = by_id.get(cid)
            if ce is None:
                continue
            if ce.kind not in ("network_request", "network_response"):
                continue
            ce_i = event_index.get(ce.event_id or "", -1)
            if ce_i < 0:
                continue
            if ce_i < cluster_start_i:
                request_ev = ce
                break

        if request_ev is None:
            continue
        primary = cluster.primary_target_event_id
        if primary:
            out[primary] = (parent_event, request_ev)
    return out


def _build_set_selection_spec(
    cluster: SemanticCluster,
    events: list[TraceEvent],
    causality: dict[str, Any],
    ev: TraceEvent,
) -> tuple[SetSelectionSpec, list[str], str]:
    """WI-19: derive a SetSelectionSpec from a set_selection cluster.

    Returns (spec, target_labels, param_name).

    id+label sprint (2026-05-28): the returned ``target_labels`` are the
    human LABELS of the final-selected items (used for the param's
    example/enum so a human reads "Argentina"), NOT the opaque ids. The
    spec's ``known_options`` carries the full universe seen at record
    time (every option row that rendered, unioned from each contributing
    event's ``options_seen``). ``item_labels`` (id->label) stays as the
    selected subset for back-compat. param_name is the inferred name for
    the list param (from the prefix's last segment:
    'multiselect-categories' -> 'categories').
    """
    _ = events
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    prefix = _multiselect_prefix(ev.fingerprint)
    # Compute the param name from the prefix: drop a 'multiselect-'
    # head if present; replace '-' with '_'.
    pname_base = prefix or "items"
    pname_base = re.sub(r"^multiselect[-_]?", "", pname_base, flags=re.I)
    pname = re.sub(r"[^a-zA-Z0-9_]+", "_", pname_base).strip("_").lower() or "items"

    open_fp: Optional[ElementFingerprint] = None
    search_fp: Optional[ElementFingerprint] = None
    checkbox_template_fp: Optional[ElementFingerprint] = None
    target_items: list[str] = []
    # Real-portal cases A/B: id -> display label, captured from each
    # checkbox/item click so the runner can type the LABEL into the
    # search box (the server search is label-indexed, not id-indexed).
    item_labels: dict[str, str] = {}
    # id+label sprint: the full option universe seen at record time,
    # unioned from every contributing event's options_seen (grabber
    # captures all rendered rows on each option click + search input).
    # id -> label, first-label-wins; later turned into known_options.
    known_options_map: dict[str, str] = {}

    def _absorb_options_seen(seen: Optional[list[Any]]) -> None:
        """Fold one event's options_seen into the running universe."""
        if not seen:
            return
        for opt in seen:
            # opt is an OptionSnapshot (value=id, label=label) after
            # TraceEvent validation; tolerate a raw dict for safety.
            if isinstance(opt, dict):
                oid = opt.get("value")
                olabel = opt.get("label")
            else:
                oid = getattr(opt, "value", None)
                olabel = getattr(opt, "label", None)
            if oid is None:
                continue
            oid = str(oid)
            olabel = str(olabel).strip() if olabel else ""
            if oid not in known_options_map or (
                not known_options_map[oid] and olabel
            ):
                known_options_map[oid] = olabel or oid

    for eid in cluster.raw_event_ids:
        e = by_id.get(eid)
        if e is None:
            continue
        # Absorb the option universe from ANY event in the cluster that
        # carried one (toggle-open click, search input_change, option
        # click) -- the superset of everything the operator surfaced.
        _absorb_options_seen(getattr(e, "options_seen", None))
        role = _multiselect_role(e.fingerprint)
        if role == "toggle" and e.kind == "click" and open_fp is None:
            open_fp = e.fingerprint
        elif role == "search" and search_fp is None:
            search_fp = e.fingerprint
        elif role in ("checkbox", "item") and e.kind == "click":
            item_id = _multiselect_item_id(e.fingerprint)
            if item_id is not None:
                # Add OR toggle-remove. If the operator clicked twice
                # the second click un-toggles; we model the FINAL state
                # by tracking xor presence.
                if item_id in target_items:
                    target_items.remove(item_id)
                else:
                    target_items.append(item_id)
                # Capture the display label for this id. Prefer the
                # accessible name (computed per WAI-ARIA), then trimmed
                # text, then aria_label; fall back to the id itself so
                # the runner always has *something* to type. Always
                # record (even on toggle-remove) so a later re-add of
                # the same id keeps its label.
                label = _multiselect_item_label(e.fingerprint, item_id)
                item_labels[item_id] = label
                # The clicked option is part of the universe too -- make
                # sure it's in known_options even if options_seen missed
                # it (older grabber / single-render race).
                if item_id not in known_options_map or not known_options_map[item_id]:
                    known_options_map[item_id] = label
                # Build a template fingerprint from this event (first
                # one wins, the {item} placeholder is derived by WI-11
                # template pass).
                if checkbox_template_fp is None:
                    checkbox_template_fp = e.fingerprint

    # Real-portal case B: when the picker exposes a search box (a search
    # role event was observed in the cluster) we ALWAYS prefer
    # search-to-narrow at replay, regardless of how the operator reached
    # each option. No search box => direct (locator + scroll_into_view
    # fallback in the runner; real-portal case A).
    select_strategy = "search" if search_fp is not None else "direct"

    # Popover/listbox container: derive from the picker prefix when we
    # have one (MultiSelect.jsx renders {prefix}-popover with
    # role=listbox); else a scoped role=listbox under the picker.
    option_list_selector: Optional[str] = None
    if prefix:
        option_list_selector = f"[data-testid='{prefix}-popover']"

    # id+label sprint: known_options is the full universe (superset of
    # the selected item_labels). Preserve insertion order for stable
    # JSON; ids the operator selected but options_seen never surfaced
    # are already folded in via the checkbox-click path above.
    known_options = [
        OptionSnapshot(value=oid, label=olabel or oid)
        for oid, olabel in known_options_map.items()
    ]

    spec = SetSelectionSpec(
        mode="replace",
        param=pname,
        open_picker_fp=open_fp,
        search_fp=search_fp,
        checkbox_template_fp=checkbox_template_fp,
        commit_fp=None,  # toggle close uses same open_fp
        # id+label sprint (Layer B fix): the chip read must match ONLY
        # the chip element ({prefix}-chip-{id}), NOT its remove sub-
        # button ({prefix}-chip-{id}-remove) which also starts with the
        # same prefix. Without the :not(...-remove) guard the read
        # returned ['zw', 'zw-remove'] and the equality assertion failed
        # even though Zimbabwe was correctly selected. Exclude the
        # remove button so the chip set is clean.
        current_items_selector=(
            f"[data-testid^='{prefix}-chip-']"
            f":not([data-testid$='-remove'])"
            if prefix else None
        ),
        current_items_id_attr="data-testid",
        current_items_id_prefix=(
            f"{prefix}-chip-" if prefix else None
        ),
        item_labels=item_labels,
        known_options=known_options,
        select_strategy=select_strategy,  # type: ignore[arg-type]
        option_list_selector=option_list_selector,
        final_equality_assertion=True,  # WI-19 safe default
    )
    # id+label sprint: return the human LABELS of the selected items (not
    # ids) so the declared param's example/enum reads "Argentina", and
    # replay values are labels. Fall back to the id when a label is
    # missing.
    target_labels = [
        item_labels.get(item_id, item_id) for item_id in target_items
    ]
    return spec, target_labels, pname


def _detect_date_select_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-21: detect native date picker changes (the easy case) and
    custom calendar-grid click bursts.

    Two cluster shapes:
      (a) Native: a single input_change on a control whose
          control_kind is one of date_input / datetime_input /
          time_input / month_input / week_input.
      (b) Custom: a sequence of clicks inside a calendar-grid (role=
          grid + role=gridcell, OR test_id pattern '*-calendar-*' /
          '*-day-*' / '*-cell-*'). For WI-21 the detector emits a
          custom cluster anchoring on the LAST cell click (which
          carries the chosen date in its accessible_name / text).

    For native pickers we use the LAST input_change on the same field
    as the primary target (operator may have typed a date manually
    OR via the calendar UI; both fire input_change).
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []

    # (a) Native date inputs.
    for ev in events:
        if ev.event_id is None or ev.event_id in consumed:
            continue
        if ev.kind != "input_change" or ev.event_id not in user_actions:
            continue
        fp = ev.fingerprint
        if fp is None or fp.control_kind not in (
            "date_input", "datetime_input", "time_input",
            "month_input", "week_input",
        ):
            continue
        consumed.add(ev.event_id)
        clusters.append(
            SemanticCluster(
                raw_event_ids=[ev.event_id],
                cluster_kind="date_select",
                primary_target_event_id=ev.event_id,
                confidence=1.0,
                alternatives_considered=["single_event"],
            )
        )

    # (b) Custom calendar-grid clicks. The grabber's WI-03
    # control_kind doesn't carry a 'calendar' marker today, so we
    # detect via the conjunction of:
    #   - role == 'gridcell' (strong signal), AND
    #   - a test_id pattern that suggests calendar context
    #     ('day' / 'calendar' / a -day-N / -calendar- substring).
    # Requiring BOTH avoids false-positive clustering of generic
    # 'cell-...' testids (e.g. table data cells, edit-cell buttons).
    # The acceptance bar here is conservative -- when in doubt the
    # click stays as a regular click step and the operator can
    # re-annotate to date_select if needed.
    calendar_tid_pattern = re.compile(r"(day|calendar)", re.I)
    for ev in events:
        if ev.event_id is None or ev.event_id in consumed:
            continue
        if ev.kind != "click" or ev.event_id not in user_actions:
            continue
        fp = ev.fingerprint
        if fp is None:
            continue
        role_is_gridcell = (fp.role or "").lower() == "gridcell"
        tid_calendar_match = bool(
            fp.test_id and calendar_tid_pattern.search(fp.test_id)
        )
        # Strong signal #1: role=gridcell AND testid mentions
        # day/calendar.
        # Strong signal #2: testid explicitly mentions 'calendar' (a
        # 'calendar-day-N' or 'cal-DAY-X' kind of pattern).
        explicit_calendar = bool(
            fp.test_id and re.search(r"calendar", fp.test_id, re.I)
        )
        if not (
            (role_is_gridcell and tid_calendar_match)
            or explicit_calendar
        ):
            continue
        # We treat each calendar-cell click as ONE date_select. (A
        # range picker would emit two but WI-21 scope is single
        # date.) The runner navigates by semantic date at replay, so
        # we don't need to fold the prev/next month navigation clicks
        # here -- those are noise from a replay perspective.
        consumed.add(ev.event_id)
        clusters.append(
            SemanticCluster(
                raw_event_ids=[ev.event_id],
                cluster_kind="date_select",
                primary_target_event_id=ev.event_id,
                confidence=1.0,
                alternatives_considered=["single_event"],
            )
        )
    return clusters


def _build_file_spec(ev: TraceEvent) -> Optional[FileSpec]:
    """WI-29: derive a FileSpec from a file_selected event.

    The grabber's WI-29 patch stamps ev.file_metadata with a list of
    {name, size, mime, ext}. Legacy traces only have ev.file_name; we
    fall back to a single-file metadata derived from that.

    The fingerprint carries accept (HTML accept attribute) and
    multiple (HTML attribute) from WI-03.
    """
    if ev.kind != "file_selected":
        return None
    fp = ev.fingerprint
    metas: list[FileMetadata] = []
    if ev.file_metadata:
        metas = list(ev.file_metadata)
    elif ev.file_name:
        # Legacy: derive a single FileMetadata from the recorded name.
        ext = ""
        if "." in ev.file_name:
            ext = ev.file_name[ev.file_name.rfind("."):].lower()
        metas = [
            FileMetadata(
                name=ev.file_name,
                size=None,
                mime=None,
                ext=ext,
            )
        ]
    primary = metas[0] if metas else None
    return FileSpec(
        original_name=primary.name if primary else None,
        extension=primary.ext if primary else None,
        mime_hint=primary.mime if primary else None,
        size=primary.size if primary else None,
        accept_attribute=(fp.accept if fp else None),
        multiple_flag=bool(fp.multiple) if fp else False,
        recorded_files=metas,
    )


def _detect_slider_set_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-28: detect range slider drag bursts.

    A drag emits many ``input`` events as the thumb moves, followed by
    a ``change`` event on release. The grabber attributes all of them
    to the same interaction (interaction_id shared) and stamps
    raw_event_kind=``input`` on the burst events, ``change`` on the
    commit.

    Detector logic:
      - Find consecutive user-action input_change events on the SAME
        range_slider control (fingerprint match by test_id +
        control_kind=range_slider).
      - Anchor the cluster on the LAST event in the burst (which
        carries the final committed value).
      - Fold the burst input_change events as the cluster's raw events.

    The annotator collapses the whole burst into ONE slider_set step;
    the runner sets the final value once and dispatches input + change.

    Conservative: requires control_kind=range_slider AND value to look
    numeric to avoid false-positive clustering of accidental text
    inputs that happened to fire raw_event_kind=``input`` (shouldn't
    happen post-WI-13 but guard anyway).
    """
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []

    # Group range_slider input_changes by their (test_id, element_id,
    # name) identity. We only consider USER-ACTION events; the grabber's
    # interaction-id grouping is reflected in user_actions but multiple
    # range events on the SAME slider share an interaction so they are
    # all root-attributed.
    def slider_key(ev: TraceEvent) -> Optional[str]:
        fp = ev.fingerprint
        if fp is None or fp.control_kind != "range_slider":
            return None
        # Prefer test_id; fall back to element_id / name. css_path is
        # too fragile -- a re-render can change positional selectors.
        return fp.test_id or fp.element_id or fp.name or None

    # Walk events in order, accumulating contiguous runs of slider input
    # events on the same key. A foreign event on the same key (e.g.
    # click on another control) breaks the run.
    current_key: Optional[str] = None
    current_run: list[TraceEvent] = []

    def emit_run() -> None:
        nonlocal current_run
        if len(current_run) == 0:
            return
        # Anchor on the LAST event -- it carries the committed value.
        last = current_run[-1]
        # Mark all run event ids consumed; the primary target is `last`.
        run_ids = [e.event_id for e in current_run if e.event_id]
        for rid in run_ids:
            consumed.add(rid)
        clusters.append(
            SemanticCluster(
                raw_event_ids=run_ids,
                cluster_kind="slider_set",
                primary_target_event_id=last.event_id,
                confidence=1.0,
                alternatives_considered=(
                    ["single_event"] if len(current_run) == 1 else []
                ),
            )
        )
        current_run = []

    for ev in events:
        if ev.event_id is None or ev.event_id in consumed:
            continue
        if ev.kind != "input_change" or ev.event_id not in user_actions:
            # Boundary: a non-slider user action breaks any active run.
            if current_run:
                emit_run()
                current_key = None
            continue
        k = slider_key(ev)
        if k is None:
            # Not a slider -- close any open run.
            if current_run:
                emit_run()
                current_key = None
            continue
        # Numeric-value guard: range_slider should always carry a
        # numeric value. If it doesn't, skip rather than mis-cluster.
        val = ev.value or ""
        try:
            float(val)
        except (TypeError, ValueError):
            if current_run:
                emit_run()
                current_key = None
            continue

        if current_key is None or k != current_key:
            # Start a new run.
            if current_run:
                emit_run()
            current_key = k
            current_run = [ev]
        else:
            current_run.append(ev)

    # Flush any trailing run.
    if current_run:
        emit_run()

    return clusters


def _detect_shortcut_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-41: detect global keyboard shortcuts.

    The grabber emits ``kind='key'`` with raw_event_kind='shortcut'
    when a modifier+key chord or a standalone F-key / '/' / '?'
    is pressed. Each such event is its own cluster -- the operator's
    intent is one chord -> one shortcut step.

    Excludes plain Enter/Escape inside text inputs (those still
    feed the fill_submit + modal-close detectors via the legacy
    raw_event_kind='keydown' path).
    """
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []
    for ev in events:
        if ev.event_id is None or ev.event_id in consumed:
            continue
        if ev.kind != "key" or ev.event_id not in user_actions:
            continue
        if ev.raw_event_kind != "shortcut":
            continue
        consumed.add(ev.event_id)
        clusters.append(
            SemanticCluster(
                raw_event_ids=[ev.event_id],
                cluster_kind="shortcut",
                primary_target_event_id=ev.event_id,
                confidence=1.0,
                alternatives_considered=["single_event"],
            )
        )
    return clusters


def _build_shortcut_spec(ev: TraceEvent) -> Optional[ShortcutSpec]:
    """WI-41: derive a ShortcutSpec from a key event whose
    raw_event_kind='shortcut'.

    The grabber stamps shortcut_modifiers as the chord's modifiers in
    Playwright key-name form; the value carries the non-modifier key.
    Scope defaults to ``global`` -- the annotator can't reliably
    distinguish focused_element shortcuts from global ones from a
    single key event, so it leaves the operator to override when
    necessary via portal config.
    """
    if ev.kind != "key" or not ev.value:
        return None
    mods_raw = list(ev.shortcut_modifiers or [])
    # Normalize: dedupe + filter to valid modifiers.
    valid_mods: set[str] = {"Control", "Meta", "Shift", "Alt"}
    mods = [m for m in mods_raw if m in valid_mods]
    # No expected_effect can be inferred from one keystroke; the
    # operator declares it post-annotate (or it stays None and the
    # runner just presses + returns).
    return ShortcutSpec(
        modifiers=mods,  # type: ignore[arg-type]
        key=ev.value,
        scope="global",
        expected_effect=None,
    )


def _detect_rich_text_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-39: detect contenteditable / rich-text editor bursts.

    The grabber's WI-39 patch debounces typing inside a contenteditable
    root and emits ONE ``input_change`` per burst with
    raw_event_kind="rich_text_input" and rich_text_html / rich_text_framework
    populated. Each such event is its OWN cluster (single_event in
    structure) but with cluster_kind="rich_text_set" so the build_skill
    step loop knows to stamp a RichTextSpec.

    No multi-event collapsing here: the grabber already collapsed the
    burst into one event by debouncing. The detector's job is just to
    recognize the event and reserve its id, so the generic
    single_event fallback doesn't emit it as a plain change step.

    Conservative: requires the rich_text_input raw_event_kind (set by
    the grabber). Legacy traces without this field fall through to the
    plain change step (no breakage).
    """
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []
    for ev in events:
        if ev.event_id is None or ev.event_id in consumed:
            continue
        if ev.kind != "input_change" or ev.event_id not in user_actions:
            continue
        if ev.raw_event_kind != "rich_text_input":
            continue
        # Defense: a contenteditable burst SHOULD carry rich_text_html
        # OR a control_kind=contenteditable fingerprint. If neither
        # signal is present the event isn't really a rich-text burst
        # and we skip rather than mis-cluster.
        fp = ev.fingerprint
        has_ce_fp = fp is not None and fp.control_kind == "contenteditable"
        has_html = ev.rich_text_html is not None
        if not has_ce_fp and not has_html:
            continue
        consumed.add(ev.event_id)
        clusters.append(
            SemanticCluster(
                raw_event_ids=[ev.event_id],
                cluster_kind="rich_text_set",
                primary_target_event_id=ev.event_id,
                confidence=1.0,
                alternatives_considered=["single_event"],
            )
        )
    return clusters


def _build_rich_text_spec(
    ev: TraceEvent,
    param_name: str,
) -> RichTextSpec:
    """WI-39: derive a RichTextSpec from a contenteditable burst event.

    Picks the format based on whether the captured HTML diverges from
    textContent: when HTML and text are identical (modulo whitespace),
    the editor's content is plain text and we emit format="plain";
    otherwise we emit format="html" so the runner preserves markup.

    paste_strategy defaults to ``input_event`` (the most portable
    across editor frameworks); operators can override per-portal.
    embed_policy follows from format: html/markdown -> strip (the param
    carries full content), plain -> preserve (don't accidentally drop
    embedded media when editing a comment).
    """
    fp = ev.fingerprint
    html = ev.rich_text_html or ""
    text = ev.value or ""
    framework = ev.rich_text_framework or "unknown"
    # Format inference: a contenteditable that only ever contained plain
    # text (no tags beyond the framework's root <p>) is plain. Anything
    # with real tags (<strong>, <em>, embed wrappers) is html.
    # Heuristic: count non-paragraph tags after stripping the outermost
    # wrapper. < 1 -> plain; >= 1 -> html.
    fmt: Literal["html", "markdown", "plain"] = "plain"
    if html:
        # Cheap tag-count: any tag that's not <p>/<br>/<div> indicates
        # formatting the operator added. Markdown detection is out of
        # scope -- operators using a markdown editor will need to set
        # format="markdown" explicitly via the portal config.
        try:
            stripped = re.sub(r"</?(p|br|div)[^>]*>", "", html, flags=re.I)
            if re.search(r"<[a-zA-Z]", stripped):
                fmt = "html"
        except Exception:
            fmt = "plain"
    embed_policy: Literal["preserve", "strip"] = (
        "strip" if fmt in ("html", "markdown") else "preserve"
    )
    # Cap recorded snapshots so the skill JSON stays bounded.
    if len(html) > 8192:
        html_audit: Optional[str] = html[:8192] + "..."
    else:
        html_audit = html or None
    if len(text) > 4096:
        text_audit: Optional[str] = text[:4096] + "..."
    else:
        text_audit = text or None
    return RichTextSpec(
        value_param=param_name,
        format=fmt,
        embed_policy=embed_policy,
        paste_strategy="input_event",
        editor_root_fp=fp,
        framework_hint=framework,  # type: ignore[arg-type]
        recorded_html=html_audit,
        recorded_text=text_audit,
    )


def _detect_drag_drop_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-30: detect drag/drop interactions.

    A drag interaction is: dragstart -> (dragover*) -> drop. The
    grabber attributes all three to the same interaction so the
    causality graph lists them as siblings of the same parent. The
    detector pairs each ``drop`` event with the LATEST ``dragstart``
    that preceded it within the same interaction; intervening dragover
    events are folded into the cluster's raw_event_ids for audit but
    don't drive replay.

    The cluster's primary_target is the DROP event (carries the
    landing target's fingerprint + DataTransfer summary). The source
    fingerprint is read from the dragstart's fingerprint at build-spec
    time.
    """
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []

    # Walk events; remember the most recent unconsumed dragstart and
    # any dragover events accumulated since.
    pending_dragstart: Optional[TraceEvent] = None
    pending_dragovers: list[TraceEvent] = []

    for ev in events:
        if ev.event_id is None or ev.event_id in consumed:
            continue
        if ev.kind == "dragstart":
            # A fresh dragstart abandons any prior incomplete drag
            # (operator cancelled mid-drag with Escape; not modeled
            # today, just skip).
            if pending_dragstart is not None:
                # Mark the prior dragstart as a single_event for now --
                # it has no commit. Don't claim it here; the fallback
                # loop will emit it.
                pass
            pending_dragstart = ev
            pending_dragovers = []
            continue
        if ev.kind == "dragover":
            if pending_dragstart is not None:
                pending_dragovers.append(ev)
            continue
        if ev.kind == "drop":
            if pending_dragstart is None:
                # Drop without a paired dragstart -- discard. Browsers
                # don't typically emit this, but be safe.
                continue
            # Build the cluster.
            raw_ids: list[str] = []
            if pending_dragstart.event_id:
                raw_ids.append(pending_dragstart.event_id)
                consumed.add(pending_dragstart.event_id)
            for dv in pending_dragovers:
                if dv.event_id:
                    raw_ids.append(dv.event_id)
                    consumed.add(dv.event_id)
            if ev.event_id:
                raw_ids.append(ev.event_id)
                consumed.add(ev.event_id)
            clusters.append(
                SemanticCluster(
                    raw_event_ids=raw_ids,
                    cluster_kind="drag_drop",
                    primary_target_event_id=ev.event_id,
                    confidence=1.0,
                    alternatives_considered=["single_event"],
                )
            )
            pending_dragstart = None
            pending_dragovers = []
            continue

    return clusters


def _detect_toggle_state_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
    consumed: set[str],
) -> list[SemanticCluster]:
    """WI-33: detect click events on toggle-control elements.

    A click is a toggle when the target carries one of:
      - aria-expanded (accordion / collapse / disclosure / combobox)
      - role in _TOGGLE_ROLES (button + aria-pressed)
      - aria-checked (custom checkbox / switch in ARIA)

    AND the grabber's WI-14 target_state_after captured the post-
    click state. The desired state at replay is target_state_after.
    """
    user_actions: set[str] = set(causality.get("user_actions") or [])
    clusters: list[SemanticCluster] = []
    for ev in events:
        if ev.event_id is None or ev.event_id in consumed:
            continue
        if ev.kind != "click" or ev.event_id not in user_actions:
            continue
        fp = ev.fingerprint
        if fp is None:
            continue
        # The element must have at least one toggle state attribute
        # captured. aria_expanded is the strongest signal (accordion
        # /disclosure); aria_pressed and aria_checked also count.
        has_expanded = fp.aria_expanded is not None
        # The target_state_after must carry the post-click state for
        # us to derive a desired target_state at replay.
        if not has_expanded:
            continue
        if ev.target_state_after is None:
            continue
        consumed.add(ev.event_id)
        clusters.append(
            SemanticCluster(
                raw_event_ids=[ev.event_id],
                cluster_kind="toggle_state",
                primary_target_event_id=ev.event_id,
                confidence=1.0,
                alternatives_considered=["single_event"],
            )
        )
    return clusters


def _build_toggle_state_spec(
    ev: TraceEvent,
) -> Optional[ToggleStateSpec]:
    """WI-33: derive a ToggleStateSpec from a click event whose
    target carried aria-expanded (or similar) and whose
    target_state_after captured the post-click state."""
    fp = ev.fingerprint
    if fp is None or ev.target_state_after is None:
        return None
    after = ev.target_state_after
    # Determine the desired target_state. Prefer aria_expanded after
    # the click; fall back to aria_pressed / aria_checked / 'open'
    # data-state.
    target_state: Optional[bool] = None
    state_attribute: Literal[
        "aria-expanded", "aria-pressed", "aria-checked", "data-state"
    ] = "aria-expanded"
    if "aria_expanded" in after:
        v = after["aria_expanded"]
        target_state = (
            v if isinstance(v, bool)
            else str(v).lower() == "true"
        )
        state_attribute = "aria-expanded"
    elif "aria_pressed" in after:
        v = after["aria_pressed"]
        target_state = (
            v if isinstance(v, bool)
            else str(v).lower() == "true"
        )
        state_attribute = "aria-pressed"
    elif "aria_checked" in after:
        v = after["aria_checked"]
        target_state = (
            v if isinstance(v, bool)
            else str(v).lower() == "true"
        )
        state_attribute = "aria-checked"
    if target_state is None:
        # Fall back to the fingerprint's aria_expanded BEFORE the
        # click and invert (a toggle click flips the state).
        if fp.aria_expanded is not None:
            target_state = not fp.aria_expanded
            state_attribute = "aria-expanded"
        else:
            return None
    return ToggleStateSpec(
        target_state=target_state,
        state_attribute=state_attribute,
        # controlled_panel_selector: future enhancement reads aria-
        # controls from the grabber's ancestor_chain to derive the
        # panel selector. For now None means runner verifies via the
        # state attribute alone.
        controlled_panel_selector=None,
    )


def _build_drag_drop_spec(
    cluster: SemanticCluster,
    events: list[TraceEvent],
    drop_ev: TraceEvent,
) -> Optional["DragDropSpec"]:
    """WI-30: derive a DragDropSpec from the cluster's dragstart +
    drop pair.

    The cluster's raw_event_ids list begins with the dragstart and
    ends with the drop; we look up the dragstart fingerprint as the
    source and use the drop event's drop_target_fp (or fallback to
    its fingerprint) as the target. The DataTransfer summary is
    pulled from the drop event.
    """
    _ = cluster
    # Find the dragstart by walking the cluster's raw_event_ids in
    # order; the first dragstart is the source.
    source_fp: Optional[ElementFingerprint] = None
    by_id = {e.event_id: e for e in events if e.event_id}
    for raw_id in cluster.raw_event_ids:
        e = by_id.get(raw_id)
        if e is not None and e.kind == "dragstart":
            source_fp = e.fingerprint
            break
    if source_fp is None:
        return None
    target_fp = drop_ev.drop_target_fp or drop_ev.fingerprint
    if target_fp is None:
        return None
    # Drop effect from the DataTransfer summary, default move.
    drop_effect: Literal["move", "copy", "link", "none"] = "move"
    if drop_ev.data_transfer_summary:
        de = (
            drop_ev.data_transfer_summary.get("drop_effect") or "move"
        ).lower()
        if de in ("move", "copy", "link", "none"):
            drop_effect = de  # type: ignore[assignment]
    return DragDropSpec(
        source_fp=source_fp,
        target_fp=target_fp,
        data_payload_summary=drop_ev.data_transfer_summary,
        drop_effect=drop_effect,
        coordinates_policy="center",
        use_high_level_api=True,
    )


def _build_slider_set_spec(
    cluster: SemanticCluster,
    events: list[TraceEvent],
    causality: dict[str, Any],
    ev: TraceEvent,
    param_name: str,
) -> SliderSpec:
    """WI-28: derive a SliderSpec from the last (committed) event in
    the drag burst. The grabber captured min/max/step on the
    fingerprint at L907-909 of grabber.js."""
    _ = cluster
    _ = events
    _ = causality
    fp = ev.fingerprint

    def _to_float(s: Optional[str]) -> Optional[float]:
        if s is None or s == "":
            return None
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    return SliderSpec(
        value_param=param_name,
        min=_to_float(fp.min if fp else None),
        max=_to_float(fp.max if fp else None),
        step=_to_float(fp.step if fp else None),
        orientation="horizontal",  # default; the grabber doesn't
        # distinguish vertical sliders today. The runner doesn't differ
        # in behavior; orientation is informational.
        event_mode="both",
        final_value=ev.value,
    )


def _build_date_select_spec(
    cluster: SemanticCluster,
    events: list[TraceEvent],
    causality: dict[str, Any],
    ev: TraceEvent,
    param_name: str,
) -> DatePickerSpec:
    """WI-21: derive a DatePickerSpec from the captured event.

    Distinguishes native (control_kind=date_input/datetime_input/...)
    from custom (a click on a gridcell / calendar testid).
    """
    _ = cluster
    _ = events
    _ = causality
    fp = ev.fingerprint
    kind: Literal["native", "custom"] = "native"
    if ev.kind == "click":
        kind = "custom"
    selected_day_identity: Optional[dict[str, Any]] = None
    if kind == "custom" and fp is not None:
        # Stash the recorded click target's identifying attrs as
        # audit-only context. The runner picks by target_date param,
        # NOT by re-replaying this click.
        selected_day_identity = {
            "test_id": fp.test_id,
            "text": fp.text,
            "accessible_name": fp.accessible_name,
        }
    return DatePickerSpec(
        kind=kind,
        value_param=param_name,
        timezone=(fp.timezone_hint if fp else None),
        display_format=None,  # populated by future WI-48 work
        calendar_grid_fp=(fp if kind == "custom" else None),
        prev_month_fp=None,
        next_month_fp=None,
        month_year_label_fp=None,
        day_cell_template_fp=(fp if kind == "custom" else None),
        selected_day_identity=selected_day_identity,
    )


def detect_semantic_clusters(
    events: list[TraceEvent],
    causality: dict[str, Any],
) -> list[SemanticCluster]:
    """WI-12 cluster detection pass.

    Walks the (already-normalized + caused) event list and emits one
    ``SemanticCluster`` per coherent operator interaction.

    Current detectors (deterministic, additive):
      - ``click_with_navigation``: a user click whose causality.children_of
        includes a navigate event. The navigate folds in (WI-08).
      - ``single_event``: every remaining user-action event (click,
        change, key, submit, navigate, file_selected, upload, wait).
        This is the floor -- the pipeline always produces at least
        one cluster per non-observed event.

    Future WIs add detectors WITHOUT touching this signature:
      - WI-15 ``fill_submit``: input_change+ Enter/submit on same form
      - WI-16 ``select_autocomplete``: input + result option click
      - WI-17 ``select_option``: select change with options snapshot
      - WI-18 ``cascading_select``: select A -> request -> select B
      - WI-19 ``set_selection``: multi-select open/search/check/close
      - WI-21 ``date_select``: native date or calendar grid
      - WI-28 ``slider_set``: range input drag burst
      - WI-30 ``drag_drop``: dragstart/dragover/drop sequence
      - WI-33 ``toggle_state``: accordion / aria-expanded transitions
      - WI-34 ``modal_open`` / ``modal_close``: dialog visibility
      - WI-35 ``popup_open``: window.open / target=_blank
      - WI-38 ``scroll_until``: scroll followed by DOM growth
      - WI-39 ``rich_text_set``: contenteditable input bursts
      - WI-45 ``download_click``: click that triggers a download effect

    Observed events (network_request, dom_mutation, ...) are NOT
    emitted as their own clusters. They live as raw_event_ids on the
    cluster that caused them (via TraceEvent.caused_by). The cluster
    detector finds them by walking causality.children_of[user_id].
    """
    user_actions: set[str] = set(causality.get("user_actions") or [])
    children_of: dict[str, list[str]] = causality.get("children_of") or {}
    by_id: dict[str, TraceEvent] = causality.get("by_id") or {}

    # Track which event ids have been folded into a cluster already so
    # we don't emit duplicates (e.g. a caused navigate that folded into
    # its click must not produce its own single_event cluster).
    folded_ids: set[str] = set()

    # WI-15+: specialized detectors run BEFORE the click_with_navigation
    # + single_event fallback. Each detector consumes the events that
    # belong to its cluster (added to folded_ids) so the fallback loop
    # below doesn't double-emit them. Detectors are ordered most-
    # specific first (fill_submit binds to a contiguous input burst;
    # the per-event fallback would emit one change step per input).
    clusters: list[SemanticCluster] = []
    # WI-39: rich_text_set runs FIRST so contenteditable bursts
    # (raw_event_kind="rich_text_input") are reserved before
    # fill_submit's generic input_change walker can claim them. The
    # detector filters strictly on raw_event_kind so it never consumes
    # plain-input events.
    clusters.extend(
        _detect_rich_text_clusters(events, causality, folded_ids)
    )
    # WI-41: shortcuts before fill_submit so a raw_event_kind='shortcut'
    # key event (Ctrl+S, Ctrl+Enter) is reserved before fill_submit's
    # trigger detection scans it. The detector filters strictly on
    # raw_event_kind so plain-key events (Enter to submit a search)
    # remain available to fill_submit.
    clusters.extend(
        _detect_shortcut_clusters(events, causality, folded_ids)
    )
    clusters.extend(
        _detect_fill_submit_clusters(events, causality, folded_ids)
    )
    clusters.extend(
        _detect_select_autocomplete_clusters(events, causality, folded_ids)
    )
    clusters.extend(
        _detect_select_option_clusters(events, causality, folded_ids)
    )
    clusters.extend(
        _detect_set_selection_clusters(events, causality, folded_ids)
    )
    clusters.extend(
        _detect_date_select_clusters(events, causality, folded_ids)
    )
    clusters.extend(
        _detect_slider_set_clusters(events, causality, folded_ids)
    )
    clusters.extend(
        _detect_drag_drop_clusters(events, causality, folded_ids)
    )
    clusters.extend(
        _detect_toggle_state_clusters(events, causality, folded_ids)
    )

    for ev in events:
        if not ev.event_id or ev.event_id in folded_ids:
            continue
        # Observed events that haven't been claimed by a parent cluster
        # are dropped from cluster output but their raw_event_ids still
        # belong to whatever caused them. The annotator only emits
        # clusters from USER ACTIONS (events with caused_by=None or
        # events the grabber marked as root via _rootAttribution).
        if ev.kind in _OBSERVED_EVENT_KINDS:
            continue
        if ev.event_id not in user_actions:
            # This is a child event whose parent is some other user
            # action -- it will be folded into that parent. Don't emit
            # a separate cluster for it. (Today this is mostly caused
            # navigates; future WIs may produce other child kinds.)
            continue

        # Click that caused a navigate -> click_with_navigation cluster.
        child_ids = children_of.get(ev.event_id, [])
        caused_nav_id: Optional[str] = None
        if ev.kind == "click":
            for cid in child_ids:
                ce = by_id.get(cid)
                if ce is not None and ce.kind == "navigate":
                    caused_nav_id = cid  # last child wins (router chain)
        # Build the cluster.
        raw_ids: list[str] = [ev.event_id]
        if caused_nav_id is not None:
            raw_ids.append(caused_nav_id)
            folded_ids.add(caused_nav_id)
        # Fold in any observed children (network / dom_mutation) so the
        # audit log carries them on the same cluster. They don't become
        # steps but they DO contribute to the provenance trail.
        for cid in child_ids:
            if cid == caused_nav_id:
                continue
            ce = by_id.get(cid)
            if ce is None:
                continue
            if ce.kind in _OBSERVED_EVENT_KINDS:
                raw_ids.append(cid)
                folded_ids.add(cid)

        kind = (
            "click_with_navigation"
            if caused_nav_id is not None
            else "single_event"
        )
        clusters.append(
            SemanticCluster(
                raw_event_ids=raw_ids,
                cluster_kind=kind,
                primary_target_event_id=ev.event_id,
                confidence=1.0,
            )
        )
    return clusters


# ---- Build Skill ----------------------------------------------------------


def build_skill(
    skill_name: str,
    events: list[TraceEvent],
    description: str = "",
    base_url: Optional[str] = None,
    portal: Optional[str] = None,
    auto: bool = False,
    console: Optional[Console] = None,
    session_id: Optional[str] = None,
    annotate_mode: Literal["linear", "semantic"] = "semantic",
) -> Skill:
    console = console or Console()

    # WI-02: backfill synthetic event_ids for legacy traces so the
    # causality graph + provenance lookups have consistent inputs
    # whether the trace was recorded pre- or post- WI-02.
    events = _assign_synthetic_ids(list(events))
    # Causality graph is computed here so future WIs (WI-08 click+nav
    # collapsing, WI-12 semantic clustering) can read it without
    # re-walking the event list.
    causality = build_causality_graph(events)

    # WI-12: produce the cluster intermediate representation when in
    # ``semantic`` mode. The cluster list is attached to the Skill for
    # audit / LLM enrichment; the existing per-event loop below still
    # produces steps (with the cluster-aware raw_event_ids stamped
    # onto each step.provenance). When ``linear`` mode is requested
    # (legacy compatibility for pre-WI-12 traces), we skip the cluster
    # pipeline and emit an empty list -- steps still get raw_event_ids
    # from their own event ids since WI-02 backfill ran.
    clusters: list[SemanticCluster] = (
        detect_semantic_clusters(events, causality)
        if annotate_mode == "semantic" else []
    )
    # Index clusters by primary target event id so the step loop can
    # pull raw_event_ids onto each step without re-walking causality.
    clusters_by_target: dict[str, SemanticCluster] = {
        c.primary_target_event_id: c
        for c in clusters
        if c.primary_target_event_id
    }

    # WI-18: cascading-select dependencies. Detect parent->child
    # select_option pairs with an intervening network_request from
    # the parent. Stamp the DependencyChain onto the PARENT step.
    cascading_chains: list[tuple[str, str, Optional[TraceEvent]]] = (
        _detect_cascading_select(events, causality, set())
        if annotate_mode == "semantic" else []
    )
    # Index parent event id -> (child event id, request event).
    cascading_by_parent: dict[str, tuple[str, Optional[TraceEvent]]] = {
        pid: (cid, req) for pid, cid, req in cascading_chains
    }
    # Index child event id -> parent event id so the param decl pass
    # can stamp depends_on at the time the child SkillParam is
    # constructed.
    cascading_by_child: dict[str, str] = {
        cid: pid for pid, cid, _ in cascading_chains
    }

    # WI-20: dependent multi-select detection. For each set_selection
    # cluster, find a prior parent select event whose change fired a
    # network request that landed before the cluster's first event.
    # The mapping is by child cluster's primary_target_event_id.
    set_selection_clusters_only = [
        c for c in clusters if c.cluster_kind == "set_selection"
    ]
    dependent_multiselect = (
        _detect_dependent_multiselect(
            events, causality, set_selection_clusters_only
        )
        if annotate_mode == "semantic" else {}
    )

    # WI-15+: when a specialized cluster (fill_submit, select_autocomplete,
    # select_option, set_selection, date_select) folded multiple events,
    # the per-event loop must SKIP the non-primary members so we don't
    # emit duplicate per-event steps. Build a set of event ids that the
    # primary step will absorb. The primary target itself is NOT in this
    # set (its loop iteration produces the collapsed step).
    cluster_folded_event_ids: set[str] = set()
    for c in clusters:
        if c.cluster_kind in (
            "fill_submit", "select_autocomplete", "select_option",
            "set_selection", "date_select", "cascading_select",
            "slider_set", "drag_drop", "toggle_state",
        ):
            for raw_id in c.raw_event_ids:
                if (
                    raw_id
                    and raw_id != c.primary_target_event_id
                    and (by_id_evt := causality.get("by_id", {}).get(raw_id))
                    and by_id_evt.kind not in _OBSERVED_EVENT_KINDS
                ):
                    cluster_folded_event_ids.add(raw_id)

    # WI-08: index navigate events by the user action that caused them.
    # A click whose event_id matches a caused-navigate's caused_by gets
    # the navigate folded into its ``effects.navigation`` instead of
    # producing a separate standalone navigate step. The standalone
    # navigate step then disappears from the skill -- replay calls
    # click() and asserts the URL matched, NEVER calls page.goto() to
    # force the URL. That removes the BLOCKER-1 wrong-asset bug.
    caused_navs = _index_caused_navigates(events, causality)
    # IDs of navigate events that are caused by a user action and
    # therefore should NOT produce their own standalone step.
    folded_nav_ids: set[str] = {
        nav.event_id  # type: ignore[misc]
        for nav in caused_navs.values()
        if nav.event_id
    }

    # WI-10: observed-readiness signals indexed by causing event id.
    # When a click/change/key/submit triggered a readiness transition
    # (aria-busy clear, disabled->enabled), the annotator turns those
    # into the step's expected_signals.dom so replay waits for the
    # declared readiness instead of the legacy spinner-by-convention.
    readiness_by_cause = _index_readiness_signals(events)
    # WI-44: server validation errors observed during the action's
    # effect window. The annotator stamps StepAssertion(kind=
    # validation_field) entries onto the causing step's assert_after
    # so replay catches the same validation failure structurally.
    validation_assertions_by_cause = _index_validation_errors(events)

    # WI-34: dialog mount/unmount observations indexed by causing
    # event id. Each entry becomes effects.modal on the causing step;
    # in-dialog interactions get their locator_scope narrowed.
    modal_effects_by_cause, modal_scope_by_event = _index_modal_effects(
        events, causality,
    )

    # WI-37: detect 'operator scrolled to find a row' patterns. Emit a
    # synthetic scroll_until step BEFORE the click step that lives
    # inside the scrolled container. The map is keyed by click
    # event_id; the per-event loop inserts the synthetic step right
    # before its index reaches the click.
    scroll_until_by_click = _index_scroll_until_steps(events, causality)

    # WI-36: index of user-action event_ids whose folded network
    # children include a destructive write (PATCH/POST/PUT/DELETE).
    # The runner gets an AuthPrecondition on these steps so it checks
    # the portal's auth_signal BEFORE the step touches the page. Read-
    # only steps (GETs / no network) stay AuthPrecondition-free.
    destructive_methods = {"PATCH", "POST", "PUT", "DELETE"}
    destructive_user_events: set[str] = set()
    children_of_user: dict[str, list[str]] = (
        causality.get("children_of") or {}
    )
    by_id_evt: dict[str, TraceEvent] = causality.get("by_id") or {}
    for user_id, child_ids in children_of_user.items():
        for cid in child_ids:
            ce = by_id_evt.get(cid)
            if ce is None:
                continue
            if ce.kind != "network_request":
                continue
            mthd = (ce.method or "GET").upper()
            if mthd in destructive_methods:
                destructive_user_events.add(user_id)
                break

    # WI-42: toast events indexed by causing event id. Each entry
    # becomes effects.toast on the causing step. The grabber attributes
    # toasts emitted during an interaction window via initiator_event_id.
    from .skill_models import ToastEffect as _ToastEff
    toast_effects_by_cause: dict[str, "_ToastEff"] = {}
    for ev in events:
        if ev.kind != "toast":
            continue
        cause_id = ev.caused_by or ev.initiator_event_id
        if not cause_id:
            continue
        # Build the ToastEffect from the toast event payload. The
        # annotator picks text_pattern as a substring of the captured
        # text (first 200 chars stripped) so the runner can
        # substring-match on replay even when the toast text varies
        # slightly (interpolated user names, timestamps).
        text = ev.toast_text or ""
        text_pattern: Optional[str] = None
        if text:
            text_pattern = text[:200]
        # Dependent actions: lift the action buttons captured by the
        # grabber. dismiss_strategy: 'click_action' when there are
        # Undo/Retry buttons; 'manual_close' for dismiss-only; 'auto'
        # otherwise.
        dependent_actions: list[dict[str, Any]] = list(
            ev.toast_action_buttons or []
        )
        has_actionable = any(
            b.get("action_kind") in ("undo", "retry", "view")
            for b in dependent_actions
        )
        has_dismiss_only = (
            all(b.get("action_kind") == "dismiss" for b in dependent_actions)
            and bool(dependent_actions)
        )
        if has_actionable:
            dismiss_strat: Optional[str] = "click_action"
        elif has_dismiss_only:
            dismiss_strat = "manual_close"
        else:
            dismiss_strat = "auto"
        toast_effects_by_cause[cause_id] = _ToastEff(
            level=ev.toast_level,  # type: ignore[arg-type]
            text_pattern=text_pattern,
            dismiss_strategy=dismiss_strat,  # type: ignore[arg-type]
            dependent_actions=dependent_actions,
            message_matcher=text_pattern,  # back-compat alias
        )

    # WI-40: hover events indexed by the FOLLOWING click's event_id.
    # The grabber emits ONE hover event when a pointerenter on a menu
    # trigger revealed a submenu within the window. The annotator pairs
    # the hover with the next click event whose target descends from
    # the trigger's container, and folds the hover into the click's
    # effects.hover (HoverEffect). The hover event itself is consumed
    # (added to a skip set) and never emitted as its own step.
    hover_events_by_click: dict[str, TraceEvent] = {}
    hover_event_ids_consumed: set[str] = set()
    _pending_hover_ev: Optional[TraceEvent] = None
    for ev in events:
        if ev.kind == "hover":
            # Replace any prior unconsumed hover -- only the most
            # recent hover before a click matters.
            _pending_hover_ev = ev
            continue
        if ev.kind == "click" and _pending_hover_ev is not None:
            # Pair this click with the pending hover. We always pair
            # the most recent hover with the next click; the grabber's
            # _isHoverTrigger filter is conservative enough that
            # spurious hover events outside menu workflows are rare.
            if ev.event_id and _pending_hover_ev.event_id:
                hover_events_by_click[ev.event_id] = _pending_hover_ev
                hover_event_ids_consumed.add(_pending_hover_ev.event_id)
            _pending_hover_ev = None
            continue
        if ev.kind in ("input_change", "submit", "key", "navigate"):
            # A non-click user action invalidates a pending hover. The
            # operator hovered but did something else; the hover was
            # observational, not a menu reveal.
            _pending_hover_ev = None

    # WI-35: popup events indexed by causing event id. Each entry
    # becomes effects.popup on the causing step.
    popup_effects_by_cause: dict[str, PopupEffect] = {}
    for ev in events:
        if ev.kind != "popup":
            continue
        cause_id = ev.caused_by or ev.initiator_event_id
        if not cause_id:
            continue
        # Last-write-wins: if multiple popups attribute to the same
        # click (rare), the latest one (most likely the intended one)
        # is kept. We do not yet support multi-popup per click.
        popup_effects_by_cause[cause_id] = PopupEffect(
            url_template=None,
            window_name=ev.popup_target,
            page_binding_key=ev.popup_binding_key,
            switch_policy="switch",
            close_policy="explicit",
        )

    # WI-45: download events indexed by causing event id. The click
    # they attribute to becomes a ``download`` action (rather than a
    # plain ``click``) with a DownloadSpec attached.
    from .skill_models import DownloadSpec as _DownloadSpec
    download_specs_by_cause: dict[str, _DownloadSpec] = {}
    for ev in events:
        if ev.kind != "download":
            continue
        cause_id = ev.caused_by or ev.initiator_event_id
        if not cause_id:
            continue
        download_specs_by_cause[cause_id] = _DownloadSpec(
            filename_template=ev.download_filename,
            expected_mime=None,
            expected_min_bytes=None,
            expected_signal=None,
        )

    # WI-46: build a mapping from popup URLs to their binding_key so
    # events that occurred INSIDE a popup can be tagged with
    # page_context. We index by popup_url (the URL the popup opened
    # to) -- subsequent events whose page_url starts with that URL
    # are considered inside the popup. The grabber's add_init_script
    # installs on every page in the context, so popup-interior events
    # arrive in the same trace stream as main-page events.
    popup_url_to_binding: dict[str, str] = {}
    for ev in events:
        if ev.kind != "popup":
            continue
        if not ev.popup_url or not ev.popup_binding_key:
            continue
        popup_url_to_binding[ev.popup_url] = ev.popup_binding_key
    # Build an event -> page_binding_key map. An event is INSIDE a
    # popup when its page_url matches a known popup_url. We use a
    # prefix match because popups often navigate within their own
    # context (e.g. opens about:blank then navigates to the real URL).
    page_context_by_event: dict[str, str] = {}
    if popup_url_to_binding:
        for ev in events:
            if not ev.event_id or not ev.page_url:
                continue
            for popup_url, binding in popup_url_to_binding.items():
                # Skip about:blank popup_urls -- they match everything.
                if not popup_url or popup_url == "about:blank":
                    continue
                if (
                    ev.page_url == popup_url
                    or ev.page_url.startswith(popup_url)
                ):
                    page_context_by_event[ev.event_id] = binding
                    break

    steps: list[SkillStep] = []
    declared_params: dict[str, SkillParam] = {}
    skipped = 0
    # WI-18: event_id -> binding_name index built as we iterate.
    # Used by the per-event loop to resolve a cascading-child step's
    # depends_on to the parent event's bound param name. Parent events
    # always come BEFORE their child in event order so by the time the
    # child step builds its binding, the parent's entry is in the map.
    binding_name_by_event_id: dict[str, str] = {}
    # Track params-seen so navigation URL templates can substitute
    # values that appear in the URL (WI-08 + foundation for WI-11).
    params_seen: list[tuple[str, str]] = []
    # WI-14: prior click events seen during this build, for the
    # gesture detector (repeat detection compares against last click).
    _prior_clicks: list[TraceEvent] = []

    # F-07/WI-10: observed-event kinds (dom_mutation, network_request,
    # network_response, popup, download, visibility_change) feed the
    # annotator's structural derivation -- they are NOT user-facing
    # steps themselves. Skip them in the per-event loop so the skill
    # only carries semantic steps.
    _OBSERVED_KINDS: frozenset[str] = frozenset({
        "dom_mutation", "network_request", "network_response",
        "popup", "download", "visibility_change",
        # WI-34: dialog mount/unmount observations -- folded into the
        # causing click's effects.modal, never emitted as their own step.
        "modal",
        # WI-42: toast / snackbar observations -- folded into the
        # causing step's effects.toast.
        "toast",
    })

    for idx, ev in enumerate(events):
        # WI-08: skip caused-navigate events. They get folded into
        # their causing click below.
        if (
            ev.kind == "navigate"
            and ev.event_id
            and ev.event_id in folded_nav_ids
        ):
            continue
        # WI-10 / F-07: skip observed events; they contributed to
        # readiness_by_cause / caused_navs / future WI-09 expected
        # signal indexes but don't become steps themselves.
        if ev.kind in _OBSERVED_KINDS:
            continue
        # WI-40: hover events that the pair-with-next-click pass
        # consumed are folded into the click step's effects.hover. Skip
        # them here so they don't produce standalone steps. Unconsumed
        # hovers (no following click) also drop out -- they're
        # observational only.
        if ev.kind == "hover":
            continue

        # WI-15+: skip events that a specialized cluster folded into a
        # primary step. Without this we'd emit (e.g.) 4 change steps
        # AND one fill_submit step for a 4-keystroke search burst.
        if ev.event_id and ev.event_id in cluster_folded_event_ids:
            continue

        # WI-37: when this event is a click that was preceded by
        # operator-driven scrolling inside an ancestor scrollable
        # container, prepend a synthetic scroll_until step. The runner
        # scrolls the scroller until the target is visible BEFORE
        # clicking. This step is purely structural; it carries no
        # fingerprint of its own (the scroller's fp + target identity
        # live in the spec).
        if (
            ev.kind == "click"
            and ev.event_id
            and ev.event_id in scroll_until_by_click
        ):
            su_spec = scroll_until_by_click[ev.event_id]
            steps.append(SkillStep(
                index=len(steps),
                action="scroll_until",
                fingerprint=None,
                semantic_label=(
                    f"scroll_to_find_{su_spec.target_identity.get('test_id') or su_spec.target_identity.get('element_id') or 'target'}"
                ),
                scroll_until=su_spec,
                provenance=StepProvenance(
                    raw_event_ids=[ev.event_id],
                    cluster_kind="scroll_until_prefix",
                    detection_method="deterministic",
                ),
            ))

        label = auto_label(ev)
        action = action_for_kind(ev.kind)
        binding = infer_param_binding(ev, label)
        gate = infer_gate(label, ev)

        # WI-15+: if this event is the primary target of a specialized
        # cluster, override the action type so the per-action payload
        # below stamps the right spec onto the step.
        cluster_here: Optional[SemanticCluster] = (
            clusters_by_target.get(ev.event_id) if ev.event_id else None
        )
        if cluster_here is not None:
            if cluster_here.cluster_kind == "fill_submit":
                action = "fill_submit"
            elif cluster_here.cluster_kind == "select_autocomplete":
                action = "select_autocomplete"
            elif cluster_here.cluster_kind == "select_option":
                action = "select_option"
            elif cluster_here.cluster_kind == "date_select":
                action = "date_select"
            elif cluster_here.cluster_kind == "set_selection":
                action = "set_selection"
                # id+label sprint (fix finding 3a): the cluster's
                # primary-target event is the checkbox CLICK. The legacy
                # infer_param_binding bound it as its own boolean param
                # (e.g. multiselect_country_checkbox_ar), so the step
                # declared TWO params and replay aborted needing the
                # stray one. A set_selection step is fully described by
                # its list param (declared from the spec below); the
                # checkbox click is CONSUMED by the cluster and must NOT
                # also be bound. Suppress the binding + gate here so the
                # param-binding declaration pass (and the step itself)
                # never emit the stray param.
                binding = None
                gate = False
                # Relabel the step from the mislabeled
                # "fill_multiselect_country_checkbox_ar" to a clean
                # set_selection label derived from the picker prefix.
                _ms_prefix = _multiselect_prefix(ev.fingerprint)
                if _ms_prefix:
                    label = f"set_selection_{_ms_prefix}".replace("-", "_")
                else:
                    label = "set_selection"
            elif cluster_here.cluster_kind == "slider_set":
                action = "slider_set"
            elif cluster_here.cluster_kind == "rich_text_set":
                action = "rich_text_set"
            elif cluster_here.cluster_kind == "shortcut":
                action = "shortcut"
            elif cluster_here.cluster_kind == "drag_drop":
                action = "drag_drop"
            elif cluster_here.cluster_kind == "toggle_state":
                action = "toggle_state"
            # ``cascading_select`` keeps action='change' but gets a
            # dependency_chain populated below (WI-18).

        keep = True
        if not auto:
            keep, label, binding, gate = _prompt_step(
                console, idx, len(events), ev, label, binding, gate
            )

        if not keep:
            skipped += 1
            continue

        # WI-08: when this is a click that CAUSED a navigation, fold
        # the navigate event into the click step's effects.navigation.
        effects: Optional[StepEffect] = None
        folded_nav: Optional[TraceEvent] = None
        if ev.kind == "click" and ev.event_id and ev.event_id in caused_navs:
            folded_nav = caused_navs[ev.event_id]
            nav_url = folded_nav.url
            nav_kind = _nav_kind_from_source(folded_nav.source)
            # WI-11: provenance-aware URL templating. Returns (template,
            # source). source=None means no template (literal URL kept).
            url_template, url_template_source = _derive_url_template(
                nav_url, params_seen, click_fp=ev.fingerprint
            )
            # Only emit a real templated URL when a placeholder was
            # substituted; otherwise leave ``url_template`` None so the
            # second-pass (which sees the FULL param set) can attempt
            # templating with later-bound params.
            has_placeholder = bool(url_template and "{" in url_template)
            emitted_template = url_template if has_placeholder else None
            emitted_source = (
                url_template_source if has_placeholder else None
            )
            assert_url = emitted_template
            effects = StepEffect(
                navigation=NavigationEffect(
                    kind=nav_kind,  # type: ignore[arg-type]
                    url=nav_url,
                    url_template=emitted_template,
                    url_template_source=emitted_source,  # type: ignore[arg-type]
                    source=folded_nav.source or folded_nav.raw_event_kind,
                    reload_allowed=False,
                    assert_url=assert_url,
                )
            )

        # WI-34: fold modal open/close observations onto this step's
        # effects.modal when the dialog watcher attributed them to this
        # event. The runner waits for the dialog visible/hidden state
        # according to opens_on_action / closes_on_action.
        if ev.event_id and ev.event_id in modal_effects_by_cause:
            modal_eff = modal_effects_by_cause[ev.event_id]
            if effects is None:
                effects = StepEffect(modal=modal_eff)
            else:
                effects.modal = modal_eff

        # WI-35: fold popup observations onto this step's effects.popup.
        # The runner uses Playwright expect_popup + page registry to
        # switch to the new page if switch_policy="switch".
        if ev.event_id and ev.event_id in popup_effects_by_cause:
            popup_eff = popup_effects_by_cause[ev.event_id]
            if effects is None:
                effects = StepEffect(popup=popup_eff)
            else:
                effects.popup = popup_eff

        # WI-45: if this click had a download intent, mark the step's
        # action as ``download`` and attach the DownloadSpec. Stored
        # locally so the step construction below uses it; the click
        # event itself stays the primary fingerprint (the operator
        # clicked a button/link to start the download).
        _attached_download_spec = None
        if ev.event_id and ev.event_id in download_specs_by_cause:
            _attached_download_spec = download_specs_by_cause[ev.event_id]
            if action == "click":
                action = "download"

        # WI-42: fold toast observations onto this step's effects.toast.
        # On level=error the runner fails the step with toast_error;
        # on success / info / warning the toast confirms the action
        # (the runner asserts visibility via text_pattern).
        if ev.event_id and ev.event_id in toast_effects_by_cause:
            toast_eff = toast_effects_by_cause[ev.event_id]
            if effects is None:
                effects = StepEffect(toast=toast_eff)
            else:
                effects.toast = toast_eff

        # WI-40: fold the preceding hover (paired by the
        # hover_events_by_click pass above) onto this step's
        # effects.hover. The runner moves the mouse to the trigger,
        # waits for opens_submenu_selector to become visible, then
        # performs the click.
        if (
            ev.event_id
            and ev.kind == "click"
            and ev.event_id in hover_events_by_click
        ):
            hover_ev = hover_events_by_click[ev.event_id]
            if hover_ev.fingerprint is not None:
                from .skill_models import HoverEffect as _HoverEff
                hover_eff = _HoverEff(
                    target_fp=hover_ev.fingerprint,
                    dwell_ms=hover_ev.dwell_ms or 0,
                    opens_submenu_selector=hover_ev.submenu_selector,
                )
                if effects is None:
                    effects = StepEffect(hover=hover_eff)
                else:
                    effects.hover = hover_eff

        # WI-10: collect readiness signals attributed to this user
        # event so the step's expected_signals.dom gets populated.
        expected_signals: Optional[ExpectedSignals] = None
        if ev.event_id and ev.event_id in readiness_by_cause:
            expected_signals = ExpectedSignals(
                dom=list(readiness_by_cause[ev.event_id])
            )
        # WI-43: when the click target was DISABLED at record time
        # (operator waited for it to become enabled mid-recording),
        # the grabber's readiness watcher should have emitted a
        # field_enabled / disabled_until_enabled transition that we
        # picked up above. If it didn't (older recording, transition
        # happened OUTSIDE the interaction window), conservatively
        # synthesize a wait_for_field_enabled expectation on the
        # step so replay waits for the target to enable before
        # clicking. Without this, the runner's WI-43 pre-click
        # probe fails with target_disabled and surfaces the missing
        # precondition -- safer than firing a click Playwright
        # would silently no-op.
        if (
            ev.kind == "click"
            and ev.fingerprint is not None
            and ev.fingerprint.disabled is True
        ):
            has_enable_signal = False
            if expected_signals is not None:
                for dom_exp in expected_signals.dom:
                    if dom_exp.kind in ("field_enabled", "disabled_until_enabled"):
                        has_enable_signal = True
                        break
            if not has_enable_signal:
                # Build a synthesized field_enabled DomExpectation
                # targeted at the recorded fingerprint's primary
                # selector. test_id > element_id > name; xpath as
                # last resort.
                fp_t = ev.fingerprint
                synth_sel: Optional[str] = None
                if fp_t.test_id:
                    synth_sel = f'[data-testid="{fp_t.test_id}"]'
                elif fp_t.element_id:
                    synth_sel = f'#{fp_t.element_id}'
                elif fp_t.name:
                    synth_sel = f'[name="{fp_t.name}"]'
                if synth_sel:
                    synth_de = DomExpectation(
                        kind="field_enabled",
                        selector=synth_sel,
                    )
                    if expected_signals is None:
                        expected_signals = ExpectedSignals(dom=[synth_de])
                    else:
                        expected_signals.dom.insert(0, synth_de)

        # WI-12: prefer cluster-derived raw_event_ids when the cluster
        # pipeline ran (semantic mode). The cluster has already folded
        # in caused navigates AND observed children (network_request /
        # dom_mutation) so the audit trail is complete. Linear-mode
        # falls back to the WI-08 inline computation for back-compat.
        cluster_for_step: Optional[SemanticCluster] = (
            clusters_by_target.get(ev.event_id) if ev.event_id else None
        )
        if cluster_for_step is not None:
            step_raw_ids = list(cluster_for_step.raw_event_ids)
            step_cluster_kind = cluster_for_step.cluster_kind
        else:
            step_raw_ids = (
                [ev.event_id, folded_nav.event_id]  # type: ignore[list-item]
                if ev.event_id and folded_nav and folded_nav.event_id
                else ([ev.event_id] if ev.event_id else [])
            )
            step_cluster_kind = (
                "click_with_navigation"
                if folded_nav is not None
                else "single_event"
            )
        # WI-13: detect deliberate-clear transitions on text inputs.
        # The grabber's WI-13 patch captures value_before at focus;
        # derive_value_transition pairs (before, after) with a
        # commit-signal scan to mark clear_intent.
        v_transition = derive_value_transition(ev, events, idx)

        # WI-14: click gesture + effect signature classification.
        # Only applies to click events; other actions stay None.
        click_gesture: Optional[str] = None
        effect_signature: Optional[dict[str, Any]] = None
        if ev.kind == "click":
            click_gesture = classify_click_gesture(ev, _prior_clicks)
            sig = _state_diff(ev.target_state_before, ev.target_state_after)
            if sig:
                effect_signature = sig
            _prior_clicks.append(ev)

        # WI-15: build the FillSubmitSpec for fill_submit-cluster steps.
        # The primary_target is the LAST input_change (where the final
        # value lives). The trigger is identified by walking the
        # cluster's raw_event_ids and finding the event that's not an
        # input_change and not an observed event.
        fill_submit_spec: Optional[FillSubmitSpec] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "fill_submit"
        ):
            fill_submit_spec = _build_fill_submit_spec(
                cluster_here, events, causality, binding
            )

        # WI-30: build the DragDropSpec for drag_drop cluster steps.
        # The cluster's primary_target is the DROP event; the source
        # fingerprint is read from the dragstart at the head of the
        # cluster's raw_event_ids list.
        drag_drop_spec: Optional[DragDropSpec] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "drag_drop"
        ):
            drag_drop_spec = _build_drag_drop_spec(
                cluster_here, events, ev
            )

        # WI-33: build the ToggleStateSpec for toggle_state cluster
        # steps. The desired state is target_state_after (captured by
        # the grabber's WI-14 patch); the runner skips the click when
        # the current state already matches.
        toggle_state_spec: Optional[ToggleStateSpec] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "toggle_state"
        ):
            toggle_state_spec = _build_toggle_state_spec(ev)

        # WI-28: build the SliderSpec for slider_set cluster steps.
        # The cluster's primary target is the LAST event in the drag
        # burst (carries the final committed value). The spec's
        # value_param is bound from the operator's binding name or
        # falls back to a derived name from the slider's identifying
        # attribute (test_id / name / element_id).
        slider_set_spec: Optional[SliderSpec] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "slider_set"
        ):
            if binding is not None:
                sl_pname = binding.name
            elif ev.fingerprint is not None:
                raw = (
                    ev.fingerprint.name
                    or ev.fingerprint.test_id
                    or ev.fingerprint.element_id
                    or "slider"
                )
                sl_pname = re.sub(
                    r"[^a-zA-Z0-9_]+", "_", raw
                ).strip("_").lower() or "slider"
            else:
                sl_pname = "slider"
            slider_set_spec = _build_slider_set_spec(
                cluster_here, events, causality, ev, sl_pname
            )

        # WI-41: build the ShortcutSpec for shortcut cluster steps. The
        # grabber stamped modifiers + key on the event; the spec is a
        # near-pure projection. No param is declared -- shortcuts are
        # symbolic, not data-bound.
        shortcut_spec: Optional[ShortcutSpec] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "shortcut"
        ):
            shortcut_spec = _build_shortcut_spec(ev)

        # WI-39: build the RichTextSpec for rich_text_set cluster steps.
        # The grabber's WI-39 burst already collapsed the operator's
        # typing into one event; the spec carries format + paste strategy
        # + framework hint so the runner picks the right replay path.
        rich_text_spec: Optional[RichTextSpec] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "rich_text_set"
        ):
            # Param name from the operator's binding (if any) or
            # derived from the editor root's identifying attribute.
            rt_pname: str
            if binding is not None:
                rt_pname = binding.name
            elif ev.fingerprint is not None:
                raw = (
                    ev.fingerprint.test_id
                    or ev.fingerprint.element_id
                    or ev.fingerprint.name
                    or "rich_text"
                )
                rt_pname = re.sub(
                    r"[^a-zA-Z0-9_]+", "_", raw
                ).strip("_").lower() or "rich_text"
            else:
                rt_pname = "rich_text"
            rich_text_spec = _build_rich_text_spec(ev, rt_pname)

        # WI-21: build the DatePickerSpec for date_select cluster steps.
        # Distinguishes native (single input_change on a date input)
        # from custom (a click on a calendar gridcell). The spec's
        # value_param is bound from the operator's binding name or
        # defaults to a sensible name derived from the field.
        date_select_spec: Optional[DatePickerSpec] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "date_select"
        ):
            # Name the date param from the field's binding (if any)
            # or from its test_id / name attribute.
            ds_pname: str
            if binding is not None:
                ds_pname = binding.name
            elif ev.fingerprint is not None:
                raw = (
                    ev.fingerprint.name
                    or ev.fingerprint.test_id
                    or ev.fingerprint.element_id
                    or "date"
                )
                ds_pname = re.sub(
                    r"[^a-zA-Z0-9_]+", "_", raw
                ).strip("_").lower() or "date"
            else:
                ds_pname = "date"
            date_select_spec = _build_date_select_spec(
                cluster_here, events, causality, ev, ds_pname
            )

        # WI-19: build the SetSelectionSpec for set_selection cluster
        # steps. The cluster collapsed toggle/search/checkbox/close
        # events; we lift the open/search/checkbox-template
        # fingerprints into the spec and stamp the recording's target
        # items list into self.params for the runner to consume.
        set_selection_spec: Optional[SetSelectionSpec] = None
        set_selection_target_items: list[str] = []
        set_selection_param_name: Optional[str] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "set_selection"
        ):
            (
                set_selection_spec,
                set_selection_target_items,
                set_selection_param_name,
            ) = _build_set_selection_spec(
                cluster_here, events, causality, ev
            )
            # WI-20: overlay dependent-multiselect metadata onto the
            # child set_selection spec when a parent picker drove its
            # options. depends_on, parent_picker_fp, option_source.
            if ev.event_id and ev.event_id in dependent_multiselect:
                parent_ev, req_ev = dependent_multiselect[ev.event_id]
                if parent_ev is not None:
                    # Resolve parent's param name via the running index.
                    parent_pname = (
                        binding_name_by_event_id.get(parent_ev.event_id or "")
                        if parent_ev.event_id else None
                    )
                    set_selection_spec.depends_on = parent_pname
                    set_selection_spec.parent_picker_fp = parent_ev.fingerprint
                if req_ev is not None and req_ev.url:
                    try:
                        url_pattern = req_ev.url.split("?", 1)[0]
                    except Exception:
                        url_pattern = req_ev.url
                    method = (req_ev.method or "GET").upper()
                    if method not in (
                        "GET", "POST", "PATCH", "PUT", "DELETE"
                    ):
                        method = "GET"
                    set_selection_spec.option_source = NetworkExpectation(
                        url_pattern=url_pattern,
                        method=method,  # type: ignore[arg-type]
                        optional=False,
                    )
                # hierarchy_path: for now, two-level (parent name ->
                # child param name). Future hierarchical pickers will
                # extend this when deeper trees are recorded.
                if parent_ev is not None and set_selection_param_name:
                    parent_pname = (
                        binding_name_by_event_id.get(parent_ev.event_id or "")
                        if parent_ev.event_id else None
                    )
                    if parent_pname:
                        set_selection_spec.hierarchy_path = [
                            parent_pname, set_selection_param_name,
                        ]

        # WI-17: build the SelectOptionSpec for select_option cluster
        # steps. The recording's options_snapshot is captured on the
        # fingerprint; we lift it into the spec so the runner has
        # access to the option set for fail-listing without re-reading
        # the DOM. NO fuzzy fallback: match_mode defaults to "value".
        select_option_spec: Optional[SelectOptionSpec] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "select_option"
        ):
            select_option_spec = _build_select_option_spec(
                cluster_here, events, causality, ev
            )

        # WI-18: cascading-select dependency. If this select is a
        # parent in a detected chain, attach a DependencyChain. The
        # child param's name and the option_source_request URL are
        # resolved here so the step carries the full contract.
        dependency_chain_spec: Optional[DependencyChain] = None
        if ev.event_id and ev.event_id in cascading_by_parent:
            child_eid, req_ev = cascading_by_parent[ev.event_id]
            child_ev = causality.get("by_id", {}).get(child_eid)
            # Parent param name is bound to THIS event; child param
            # name comes from the child event's binding (if any) or
            # falls back to the child's fingerprint test_id.
            child_param_name: Optional[str] = None
            if child_ev is not None:
                child_binding = infer_param_binding(
                    child_ev, auto_label(child_ev)
                )
                if child_binding is not None:
                    child_param_name = child_binding.name
                elif child_ev.fingerprint is not None:
                    raw = (
                        child_ev.fingerprint.name
                        or child_ev.fingerprint.test_id
                        or child_ev.fingerprint.element_id
                        or "child"
                    )
                    child_param_name = re.sub(
                        r"[^a-zA-Z0-9_]+", "_", raw
                    ).strip("_").lower() or "child"
            parent_param_name = binding.name if binding else None
            if parent_param_name and child_param_name:
                opt_source: Optional[NetworkExpectation] = None
                if req_ev is not None and req_ev.url:
                    try:
                        url_pattern = req_ev.url.split("?", 1)[0]
                    except Exception:
                        url_pattern = req_ev.url
                    method = (req_ev.method or "GET").upper()
                    if method not in (
                        "GET", "POST", "PATCH", "PUT", "DELETE"
                    ):
                        method = "GET"
                    opt_source = NetworkExpectation(
                        url_pattern=url_pattern,
                        method=method,  # type: ignore[arg-type]
                        optional=False,
                    )
                # Use child's fingerprint test_id as the option-
                # signature selector when available.
                child_sig: Optional[str] = None
                if child_ev is not None and child_ev.fingerprint is not None:
                    if child_ev.fingerprint.test_id:
                        child_sig = (
                            f"[data-testid='{child_ev.fingerprint.test_id}']"
                        )
                dependency_chain_spec = DependencyChain(
                    parent_param=parent_param_name,
                    child_param=child_param_name,
                    option_source_request=opt_source,
                    child_options_signature_after=child_sig,
                )

        # WI-16: build the AutocompleteSpec for select_autocomplete
        # cluster steps. The primary_target is the CLICK on the result;
        # the spec carries the separated query / selected_item params,
        # the query input fingerprint, the option identity template
        # (the click target fingerprint), and the network expectation
        # of the search request URL.
        select_autocomplete_spec: Optional[AutocompleteSpec] = None
        # Track the query example for param declaration outside the loop.
        autocomplete_query_value: Optional[str] = None
        if (
            cluster_here is not None
            and cluster_here.cluster_kind == "select_autocomplete"
        ):
            (
                select_autocomplete_spec,
                autocomplete_query_value,
                _query_input_fp,
            ) = _build_select_autocomplete_spec(
                cluster_here, events, causality,
                click_binding_name=binding.name if binding else None,
            )

        # WI-29: build the FileSpec for file_selected events. The
        # SkillStep's action stays ``upload`` (mapped by action_for_kind
        # from kind=``file_selected``); file_spec carries the recorded
        # metadata + accept/multiple constraints for the runner to
        # validate the replay-time path before set_input_files runs.
        file_spec_value = (
            _build_file_spec(ev) if ev.kind == "file_selected" else None
        )

        # WI-36: stamp an AuthPrecondition on destructive steps so the
        # runner verifies the portal's auth_signal before the step
        # touches the page. Conservative default refresh_strategy is
        # "navigate" -- the operator opens the login URL manually.
        # Annotator emits this UNCONDITIONALLY on destructive steps;
        # the runner skips the check when the portal context has no
        # auth_signal configured.
        auth_precondition_value: Optional[AuthPrecondition] = None
        if ev.event_id and ev.event_id in destructive_user_events:
            auth_precondition_value = AuthPrecondition(
                refresh_strategy="navigate",
            )

        # WI-34: when this user event happened while a dialog was open,
        # scope locator lookups inside the dialog so the runner doesn't
        # accidentally bind to a same-named control on the backing
        # page. Skipped when the event IS the OPENER -- the opening
        # click's target lives outside the dialog by definition. The
        # closer's target lives INSIDE the dialog (in-dialog click) or
        # is keyboard/backdrop (no fingerprint scope needed), so we
        # still set scope on closers and let the ambiguity policy
        # apply when there's a fingerprint to resolve.
        dialog_ambiguity_policy: Optional[AmbiguityPolicy] = None
        if (
            ev.event_id
            and ev.event_id in modal_scope_by_event
            and not (
                ev.event_id in modal_effects_by_cause
                and modal_effects_by_cause[ev.event_id].opens_on_action
                and not modal_effects_by_cause[ev.event_id].closes_on_action
            )
        ):
            dialog_ambiguity_policy = AmbiguityPolicy(
                locator_scope=modal_scope_by_event[ev.event_id],
                expected_candidate_count=1,
                ambiguity_policy="fail_if_multiple",
            )

        # WI-44: lift any validation_field assertions captured during
        # this event's effect window onto the step's assert_after.
        step_assertions: list[Any] = []
        if ev.event_id and ev.event_id in validation_assertions_by_cause:
            step_assertions.extend(
                validation_assertions_by_cause[ev.event_id]
            )

        step = SkillStep(
            index=len(steps),
            action=action,
            fingerprint=ev.fingerprint,
            assert_after=step_assertions,
            url=ev.url if ev.kind == "navigate" else None,
            value=(ev.value if ev.kind in ("input_change", "key") else None),
            file_path=(ev.file_name if ev.kind == "file_selected" else None),
            semantic_label=label,
            param_binding=binding,
            requires_gate=gate,
            captured_at=ev.ts,
            screenshot_path=ev.screenshot_path,
            effects=effects,
            expected_signals=expected_signals,
            value_transition=v_transition,
            click_gesture=click_gesture,  # type: ignore[arg-type]
            effect_signature=effect_signature,
            fill_submit=fill_submit_spec,
            select_autocomplete=select_autocomplete_spec,
            select_option=select_option_spec,
            set_selection=set_selection_spec,
            date_select=date_select_spec,
            slider_set=slider_set_spec,
            rich_text=rich_text_spec,
            shortcut=shortcut_spec,
            file_spec=file_spec_value,
            drag_drop=drag_drop_spec,
            toggle_state=toggle_state_spec,
            download_spec=_attached_download_spec,
            page_context=(
                PageContext(
                    page_binding_key=page_context_by_event[ev.event_id],
                    opens_via="popup_event",
                    expected_close="auto",
                )
                if ev.event_id
                and ev.event_id in page_context_by_event
                else None
            ),
            dependency_chain=dependency_chain_spec,
            ambiguity_policy=dialog_ambiguity_policy,
            auth_precondition=auth_precondition_value,
            # WI-02 + WI-08 + WI-12: link the step back to its source
            # raw events. The cluster pipeline (semantic mode) supplies
            # the complete list including folded observed children;
            # linear mode falls back to the click+nav pair.
            provenance=StepProvenance(
                raw_event_ids=step_raw_ids,
                cluster_kind=step_cluster_kind,
                detection_method="deterministic",
            ),
        )
        steps.append(step)

        # WI-21: declare a date param when the step is date_select and
        # no existing binding already declared it.
        if (
            date_select_spec is not None
            and date_select_spec.value_param not in declared_params
        ):
            declared_params[date_select_spec.value_param] = SkillParam(
                name=date_select_spec.value_param,
                type="date",
                codec="iso_date",  # WI-05 codec normalizes any locale to ISO
                description=(
                    f"Date for step {step.index}: {label}"
                ),
                example=(ev.value if ev.kind == "input_change" else None),
                required=True,
            )

        # WI-28: declare a number_range param for slider_set steps with
        # min/max captured from the slider's HTML constraints. The
        # runner enforces these via ParamConstraints BEFORE setting the
        # value on the page so out-of-range values surface as
        # ``param_validation_failed`` instead of being silently clamped
        # by the browser.
        if (
            slider_set_spec is not None
            and slider_set_spec.value_param not in declared_params
        ):
            constraints: Optional[ParamConstraints] = None
            if (
                slider_set_spec.min is not None
                or slider_set_spec.max is not None
            ):
                constraints = ParamConstraints(
                    min=slider_set_spec.min,
                    max=slider_set_spec.max,
                )
            declared_params[slider_set_spec.value_param] = SkillParam(
                name=slider_set_spec.value_param,
                type="number_range",
                codec="raw",  # numeric stringification handled by runner
                description=(
                    f"Slider value for step {step.index}: {label}"
                ),
                example=slider_set_spec.final_value,
                required=True,
                constraints=constraints,
            )

        # WI-39: declare a string param for rich_text_set steps. The
        # captured textContent is the example; the runner sets either
        # innerHTML or textContent at replay depending on the spec's
        # format. Param is typed as string (free text); the codec
        # remains raw because the editor's own sanitizer handles
        # markup at replay time.
        if (
            rich_text_spec is not None
            and rich_text_spec.value_param not in declared_params
        ):
            declared_params[rich_text_spec.value_param] = SkillParam(
                name=rich_text_spec.value_param,
                type="string",
                codec="raw",
                description=(
                    f"Rich text content for step {step.index}: {label}"
                ),
                example=(
                    rich_text_spec.recorded_text
                    or rich_text_spec.recorded_html
                    or ""
                ) or None,
                required=True,
            )

        # WI-19 + id+label sprint: declare a string_list param for
        # set_selection steps. ``set_selection_target_items`` now holds
        # the human LABELS of the selected items (not ids), so the
        # example reads "Argentina" and replay values are labels. The
        # param also carries enum_options = the full option universe
        # (known_options) so the planner / replay UI can offer the
        # operator the labels seen at record time. The runner resolves
        # label->id internally via the spec's known_options.
        if (
            set_selection_spec is not None
            and set_selection_param_name
            and set_selection_param_name not in declared_params
        ):
            ms_enum_options = (
                list(set_selection_spec.known_options)
                if set_selection_spec.known_options
                else None
            )
            declared_params[set_selection_param_name] = SkillParam(
                name=set_selection_param_name,
                type="string_list",
                codec="raw",
                description=(
                    f"Multi-select items (by label) for step "
                    f"{step.index}: {label}"
                ),
                example=", ".join(set_selection_target_items) or None,
                enum_options=ms_enum_options,
                required=True,
            )

        # WI-16: declare separate skill params for autocomplete steps.
        # The cluster's primary_target is the CLICK; ``binding`` here
        # carries the selected_item (from the click target). We also
        # need to declare the ``query`` param from the typed-in value.
        if select_autocomplete_spec is not None:
            if (
                autocomplete_query_value
                and select_autocomplete_spec.query_param not in declared_params
            ):
                declared_params[select_autocomplete_spec.query_param] = SkillParam(
                    name=select_autocomplete_spec.query_param,
                    type="string",
                    codec="raw",
                    description=(
                        f"Autocomplete query for step {step.index}: "
                        f"{label}"
                    ),
                    example=autocomplete_query_value,
                    required=True,
                )
            # The selected_item param. A click event doesn't produce a
            # ParamBinding via the legacy infer_param_binding (which only
            # binds input_change / file_selected), so we declare the
            # selected_item param explicitly here. The example is the
            # clicked target's identifying attribute (test_id or text)
            # so the operator can see what was originally picked.
            sel_name = select_autocomplete_spec.selected_item_param
            if sel_name and sel_name not in declared_params:
                sel_example: Optional[str] = None
                if ev.fingerprint is not None:
                    sel_example = (
                        ev.fingerprint.test_id
                        or ev.fingerprint.accessible_name
                        or ev.fingerprint.text
                    )
                declared_params[sel_name] = SkillParam(
                    name=sel_name,
                    type="string",
                    codec="raw",
                    description=(
                        f"Autocomplete selected item for step "
                        f"{step.index}: {label}"
                    ),
                    example=sel_example,
                    required=True,
                )

        # WI-08: track every (param_name, recorded_value) pair so later
        # click steps can template their navigation URL. First-seen
        # wins to keep the template stable when the same param shows
        # up in multiple steps' values.
        if binding and (ev.value or ev.file_name):
            v = str(ev.value or ev.file_name or "")
            if v and not any(name == binding.name for name, _ in params_seen):
                params_seen.append((binding.name, v))

        if binding and binding.name not in declared_params:
            example = ev.value or ev.file_name or ""
            # WI-05: deterministic typed-param inference from the
            # grabber's WI-03 control metadata. We don't override the
            # binding's legacy ``type`` (file_path stays file_path) when
            # it's already specific; we DO upgrade ``string`` bindings
            # to the more accurate type (boolean, enum, date, etc.) and
            # populate the matching codec + enum option snapshot when
            # the recording carried one. Legacy traces with no control
            # metadata still produce (string, raw) -- safe.
            fp = ev.fingerprint
            inferred_type, inferred_codec = infer_param_type_and_codec(
                control_kind=getattr(fp, "control_kind", None) if fp else None,
                value_kind=getattr(fp, "value_kind", None) if fp else None,
                recorded_value=example,
                has_options_snapshot=bool(
                    fp and fp.options_snapshot
                ) if fp else False,
            )
            # Honor an explicit file_path binding even if the grabber
            # missed control_kind (older traces). Otherwise let the
            # grabber metadata win -- it's structural truth from the
            # moment of recording.
            if binding.type == "file_path":
                # WI-29: upgrade to file_path_list when the recorded
                # file input was ``multiple``. The runner consumes
                # file_path_list as list[str] via params dict.
                if fp and fp.multiple:
                    final_type = "file_path_list"
                else:
                    final_type = "file_path"
                final_codec = "file_ref"
            else:
                final_type = inferred_type
                final_codec = inferred_codec
            # WI-18: stamp depends_on on the child param when this
            # event's id is a child in the cascading-select chain.
            depends_on_name: Optional[str] = None
            if ev.event_id and ev.event_id in cascading_by_child:
                parent_eid = cascading_by_child[ev.event_id]
                depends_on_name = binding_name_by_event_id.get(parent_eid)
            # WI-29: derive ParamConstraints from the file input's
            # accept attribute (e.g. ``image/*,.pdf`` -> mime_types=
            # ['image/'], extensions=['.pdf']).
            file_constraints: Optional[ParamConstraints] = None
            if (
                final_type in ("file_path", "file_path_list")
                and fp is not None
                and fp.accept
            ):
                mimes: list[str] = []
                exts: list[str] = []
                for tok in fp.accept.split(","):
                    t = tok.strip()
                    if not t:
                        continue
                    if t.startswith("."):
                        exts.append(t.lower())
                    elif "/" in t:
                        # MIME glob: ``image/*`` becomes prefix
                        # ``image/`` for the WI-05 constraint check.
                        mimes.append(
                            t[:-1] if t.endswith("*") else t
                        )
                if mimes or exts:
                    file_constraints = ParamConstraints(
                        mime_types=mimes or None,
                        extensions=exts or None,
                    )
            declared_params[binding.name] = SkillParam(
                name=binding.name,
                type=final_type,  # type: ignore[arg-type]
                codec=final_codec,  # type: ignore[arg-type]
                description=f"Value for step {step.index}: {label}",
                example=example or None,
                required=True,
                depends_on=depends_on_name,
                enum_options=(
                    list(fp.options_snapshot)
                    if fp and fp.options_snapshot
                    else None
                ),
                constraints=file_constraints,
            )
        if ev.event_id and binding:
            binding_name_by_event_id[ev.event_id] = binding.name

    if skipped and not auto:
        console.print(f"[dim]Skipped {skipped} event(s) marked as noise.[/dim]")

    # WI-48: derive Skill.recording_context from the most-common
    # locale_hint + timezone_hint observed across fingerprints. Each
    # fingerprint already carries WI-03's locale + timezone reads, so
    # we pick the most frequent non-None value. A skill whose
    # fingerprints disagree on locale (rare; could happen if the
    # operator switched apps mid-recording) takes the modal value.
    from .skill_models import RecordingContext as _RC
    from collections import Counter as _Counter
    _locales = _Counter()
    _timezones = _Counter()
    for ev in events:
        fp = ev.fingerprint
        if fp is None:
            continue
        if fp.locale_hint:
            _locales[fp.locale_hint] += 1
        if fp.timezone_hint:
            _timezones[fp.timezone_hint] += 1
    _ctx_locale = _locales.most_common(1)[0][0] if _locales else None
    _ctx_tz = _timezones.most_common(1)[0][0] if _timezones else None
    recording_ctx = (
        _RC(locale=_ctx_locale, timezone=_ctx_tz)
        if (_ctx_locale or _ctx_tz)
        else None
    )

    skill = Skill(
        name=skill_name,
        description=description,
        portal=portal,
        base_url=base_url,
        params=list(declared_params.values()),
        steps=steps,
        source_session_id=session_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        # WI-12: stamp the cluster pipeline output so audit / LLM
        # enrichment (WI-31) can see the cluster boundaries the
        # annotator chose without re-deriving from raw events.
        annotate_mode=annotate_mode,
        semantic_clusters=clusters,
        recording_context=recording_ctx,
    )
    # WI-11: strong-provenance templates first (row_key / operator_input);
    # legacy substring pass fills only the fields the strong pass left
    # alone, tagging itself as ``legacy_substring``. Order matters: a
    # field templated by row_key must NOT be overwritten by a coincidental
    # substring match later.
    _derive_provenance_templates(skill)
    _derive_fingerprint_templates(skill)

    # WI-11 second-pass URL templating: the per-step loop above only
    # sees params_seen accumulated up to that point, so a click step
    # that occurred BEFORE its bound input event wouldn't template its
    # navigation URL. After the loop, we have the full param set;
    # re-derive URL templates on click-with-navigation steps whose
    # url_template is still None.
    final_params_seen: list[tuple[str, str]] = []
    for s in skill.steps:
        if s.param_binding and (s.value or s.file_path):
            v = str(s.value or s.file_path or "")
            if v and len(v) >= 3 and not any(
                name == s.param_binding.name for name, _ in final_params_seen
            ):
                final_params_seen.append((s.param_binding.name, v))
    for s in skill.steps:
        if s.action != "click":
            continue
        if not s.effects or not s.effects.navigation:
            continue
        nav = s.effects.navigation
        if nav.url_template:
            continue  # already templated in the first pass
        new_tmpl, new_src = _derive_url_template(
            nav.url, final_params_seen, click_fp=s.fingerprint
        )
        # _derive_url_template returns the literal URL with source=None
        # when no provenance match is found; only update when we got a
        # placeholder template.
        if new_tmpl and "{" in new_tmpl and new_src is not None:
            nav.url_template = new_tmpl
            nav.url_template_source = new_src  # type: ignore[assignment]
            if not nav.assert_url:
                nav.assert_url = new_tmpl

    return skill


_PROVENANCE_FIELDS: tuple[str, ...] = (
    "test_id",
    "element_id",
    "css_path",
    "xpath",
    "accessible_name",
    "text",
)
"""Fingerprint fields the annotator templates. Kept in sync with the
runner's ``_materialize_fingerprint`` field list -- adding a new field
here without the runner reading it produces an orphan template that
silently has no replay effect."""


def _segment_boundary_at(literal: str, start: int, end: int) -> bool:
    """Return True iff the slice ``literal[start:end]`` sits on a token
    boundary -- i.e. the chars just before/after are not alphanumeric.
    Examples (for value ``A-9001``):
        ``btn-open-A-9001``   -> True (slash, end-of-string)
        ``btn-open-A-90015``  -> False (trailing digit attaches)
        ``A-9001-row``        -> True
        ``btn-A-9001-action`` -> True

    WI-11: this guards the substring-luck pattern flagged by the audit
    where ``A-90`` inside ``A-9001`` would template incorrectly. By
    requiring non-alphanumeric (or string boundary) on both sides we
    refuse partial-token matches.
    """
    n = len(literal)
    left_ok = start == 0 or not literal[start - 1].isalnum()
    right_ok = end >= n or not literal[end].isalnum()
    return left_ok and right_ok


def _find_unambiguous_match(
    literal: str,
    candidates: list[tuple[str, str]],
) -> Optional[tuple[str, str, int, int]]:
    """Return the unique (param_name, value, start, end) that matches
    ``literal`` on a token boundary, or None if zero or multiple
    candidates match.

    WI-11 ambiguity rule: when two different param values both occur in
    the literal AND both sit on a token boundary, the substitution is
    ambiguous (we can't tell which one is the templating intent). The
    safer behavior is to refuse templating for that field -- the literal
    locator still resolves at L1; the runner's heal layer can recover
    if the literal stops matching.
    """
    hits: list[tuple[str, str, int, int]] = []
    for name, value in candidates:
        # Search for ALL token-boundary occurrences; if any one value
        # appears more than once on a boundary, that's also ambiguous
        # (we can't pick which occurrence to template).
        per_value_hits: list[tuple[int, int]] = []
        idx = 0
        while True:
            pos = literal.find(value, idx)
            if pos < 0:
                break
            if _segment_boundary_at(literal, pos, pos + len(value)):
                per_value_hits.append((pos, pos + len(value)))
            idx = pos + 1
        if len(per_value_hits) == 1:
            hits.append((name, value, *per_value_hits[0]))
        elif len(per_value_hits) > 1:
            # Same value boundary-matches in multiple places -- ambiguous
            # placement; bail out for this literal.
            return None
    if len(hits) == 1:
        return hits[0]
    return None


def _ancestor_value_for_param(
    ancestor_chain: list[dict[str, Any]],
    param_value: str,
) -> Optional[str]:
    """Look up the param value in a fingerprint's ancestor_chain. Each
    ancestor entry has ``testId``/``id`` set by the grabber; an entry
    whose attribute ends with the param value (on a token boundary)
    counts as a row-key provenance signal.

    WI-11: a click on ``btn-open-A-9001`` whose enclosing ``<tr>`` has
    ``data-testid="catalog-row-A-9001"`` -- the ancestor's testId is
    where the row identity LIVES. Templating the click against the
    ancestor's row key is unambiguous because the click target is a
    descendant of that row.

    Returns the ancestor attribute string that matched (for logging),
    or None when no ancestor carries the param value on a boundary."""
    for anc in ancestor_chain or []:
        for attr in ("testId", "id"):
            v = anc.get(attr) if isinstance(anc, dict) else None
            if not v or not isinstance(v, str):
                continue
            pos = v.rfind(param_value)
            if pos < 0:
                continue
            if _segment_boundary_at(v, pos, pos + len(param_value)):
                return v
    return None


def _derive_provenance_templates(skill: Skill) -> tuple[int, int]:
    """WI-11: provenance-based template derivation.

    For each step's fingerprint, attempts to template each scannable
    field using the strongest available provenance:

      1. ``row_key``         -- param value appears on a token boundary
                                in an ancestor's testId/id AND in the
                                same place in this fingerprint's field.
      2. ``operator_input``  -- the recording's bound value for the param
                                came from THIS step's input; the value
                                appears unambiguously on a token boundary
                                in the field.
      3. (fallback below)    -- legacy substring matching handled by
                                _derive_fingerprint_templates with
                                template_source=legacy_substring.

    Returns ``(provenance_count, ambiguous_skipped_count)``.

    ``provenance_count`` is the number of (field) templates emitted with
    a strong source. ``ambiguous_skipped_count`` is how many fields had
    multiple candidate values and were therefore LEFT UNTEMPLATED. The
    legacy substring pass runs AFTER this and only fills fields that
    weren't already templated with a strong source -- it cannot overwrite
    a stronger provenance.

    The structured ``template_parts`` is populated alongside the raw
    ``templates`` string so future analyzers can read the literal /
    placeholder anatomy without re-parsing.
    """
    # Collect (param_name, recorded_value) pairs.
    pairs: list[tuple[str, str]] = []
    for s in skill.steps:
        if not s.param_binding:
            continue
        if s.value is None and s.file_path is None:
            continue
        recorded = s.value if s.value is not None else s.file_path
        if not recorded:
            continue
        val = str(recorded)
        if len(val) < 3:  # too short -- skip to avoid accidental matches
            continue
        pairs.append((s.param_binding.name, val))

    # Dedup, prefer longest value first (more specific matches win).
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for name, val in pairs:
        if (name, val) in seen:
            continue
        seen.add((name, val))
        unique.append((name, val))
    unique.sort(key=lambda nv: -len(nv[1]))

    if not unique:
        return (0, 0)

    provenance_count = 0
    ambiguous_skipped = 0

    for s in skill.steps:
        fp = s.fingerprint
        if fp is None:
            continue
        for field in _PROVENANCE_FIELDS:
            # Skip fields already templated by an upstream pass -- this
            # function may run more than once (annotator + tests).
            if field in fp.template_sources:
                continue
            literal = getattr(fp, field, None)
            if not literal or not isinstance(literal, str):
                continue
            # 1. row_key provenance: any param value whose ancestor chain
            #    confirms row provenance AND that value occurs on a token
            #    boundary in this field.
            row_key_winner: Optional[tuple[str, str, int, int]] = None
            for name, value in unique:
                if not _ancestor_value_for_param(fp.ancestor_chain, value):
                    continue
                # Find the unique boundary-match in this field for this
                # value. If multiple, that's ambiguous WITHIN this value
                # -- bail.
                hits: list[tuple[int, int]] = []
                idx = 0
                while True:
                    pos = literal.find(value, idx)
                    if pos < 0:
                        break
                    if _segment_boundary_at(literal, pos, pos + len(value)):
                        hits.append((pos, pos + len(value)))
                    idx = pos + 1
                if len(hits) == 1:
                    if row_key_winner is not None:
                        # Two different params both have row-key provenance
                        # AND both match this field. Ambiguous -- skip.
                        row_key_winner = None
                        ambiguous_skipped += 1
                        break
                    row_key_winner = (name, value, *hits[0])
            if row_key_winner is not None:
                name, value, start, end = row_key_winner
                _apply_template(fp, field, literal, name, start, end, "row_key")
                provenance_count += 1
                continue

            # 2. operator_input provenance: the value came from THIS
            #    step's binding (rare on the same step's fingerprint but
            #    possible on subsequent clicks that referenced an earlier
            #    input). Only emit when unambiguous across all candidate
            #    params.
            match = _find_unambiguous_match(literal, unique)
            if match is not None:
                name, value, start, end = match
                _apply_template(
                    fp, field, literal, name, start, end, "operator_input"
                )
                provenance_count += 1

    return (provenance_count, ambiguous_skipped)


def _apply_template(
    fp,
    field: str,
    literal: str,
    param_name: str,
    start: int,
    end: int,
    source: str,
) -> None:
    """Write template + template_parts + template_sources for ``field``.

    Caller has already validated that ``literal[start:end]`` is the
    param value on a token boundary; this just records the result in
    the three parallel maps."""
    placeholder = "{" + param_name + "}"
    template = literal[:start] + placeholder + literal[end:]
    fp.templates[field] = template
    fp.template_sources[field] = source  # type: ignore[assignment]
    # Build structured parts: pre-literal (if any), placeholder, post-literal
    # (if any).
    from .skill_models import TemplatePart
    parts: list = []
    if start > 0:
        parts.append(TemplatePart(kind="literal", text=literal[:start]))
    parts.append(TemplatePart(kind="placeholder", param=param_name))
    if end < len(literal):
        parts.append(TemplatePart(kind="literal", text=literal[end:]))
    fp.template_parts[field] = parts


def _derive_fingerprint_templates(skill: Skill) -> int:
    """Scan every fingerprint in the skill for substrings matching any
    recorded param value. Mark the matching fingerprint fields as
    templated so they re-render correctly at replay time when a
    different parameter value is supplied.

    Real-world example: portal renders a row's testid as
    ``row-{content_id}``. Operator records with content_id=A-9001 →
    fingerprint has ``test_id="row-A-9001"``. Without templating, replay
    with content_id=B-12345 looks for ``row-A-9001`` and fails. With
    templating, the fingerprint stores ``templates={"test_id":
    "row-{content_id}"}`` and the runner substitutes the current value
    at replay time.

    WI-11: this is the LEGACY substring pathway. It runs AFTER
    ``_derive_provenance_templates``; any field already templated with a
    strong provenance source is skipped here. Its remaining matches are
    tagged ``template_source="legacy_substring"`` so consumers (audit
    log, future provenance-aware analyzers) can distinguish strong
    templates from substring guesses.

    Substring matches now also enforce token-boundary placement (the
    same ``_segment_boundary_at`` rule used by the provenance pass) to
    reject the audit-flagged ``btn-open-{q}01`` bug where ``A-90`` inside
    ``A-9001`` would falsely template through.

    Returns the count of templated fingerprint fields detected.
    """
    # Gather every (param_name, recorded_value) pair from any step that
    # has a param binding. A single content_id may show up in many
    # steps' fingerprints (row testid, action button id, etc.), not
    # just on the step that fills it.
    pairs: list[tuple[str, str]] = []
    for s in skill.steps:
        if not s.param_binding:
            continue
        if s.value is None and s.file_path is None:
            continue
        recorded = s.value if s.value is not None else s.file_path
        if not recorded:
            continue
        # Skip very short values: too risky for substring substitution
        # (would match unrelated occurrences in unrelated DOM ids).
        if len(str(recorded)) < 3:
            continue
        pairs.append((s.param_binding.name, str(recorded)))

    # Dedup, then sort longest-value-first so substring matches prefer
    # more specific params.
    seen = set()
    unique: list[tuple[str, str]] = []
    for name, val in pairs:
        if (name, val) in seen:
            continue
        seen.add((name, val))
        unique.append((name, val))
    unique.sort(key=lambda nv: -len(nv[1]))

    templated_count = 0
    for s in skill.steps:
        fp = s.fingerprint
        if fp is None:
            continue
        for field in _PROVENANCE_FIELDS:
            # Provenance pass already claimed this field -- don't overwrite.
            if field in fp.template_sources:
                continue
            literal = getattr(fp, field, None)
            if not literal or not isinstance(literal, str):
                continue
            # WI-11 ambiguity guard: require an unambiguous, boundary-
            # respecting match. Reject otherwise to avoid the
            # ``A-90``-inside-``A-9001`` substring-luck bug.
            match = _find_unambiguous_match(literal, unique)
            if match is None:
                continue
            name, value, start, end = match
            _apply_template(fp, field, literal, name, start, end, "legacy_substring")
            templated_count += 1
    return templated_count


def _prompt_step(
    console: Console,
    idx: int,
    total: int,
    ev: TraceEvent,
    default_label: str,
    default_binding: Optional[ParamBinding],
    default_gate: bool,
) -> tuple[bool, str, Optional[ParamBinding], bool]:
    fp = ev.fingerprint
    lines = [
        f"[bold cyan]Step {idx + 1}/{total}[/bold cyan]  kind=[bold]{ev.kind}[/bold]",
    ]
    if fp:
        lines.append(f"  target: {fp.test_id or fp.accessible_name or fp.text or fp.tag}")
        if fp.landmark:
            lines.append(f"  in:     {fp.landmark}")
    if ev.value is not None:
        lines.append(f"  value:  {ev.value!r}")
    if ev.file_name:
        lines.append(f"  file:   {ev.file_name}")
    if ev.url:
        lines.append(f"  url:    {ev.url}")
    console.print(Panel("\n".join(lines), border_style="dim"))

    action = Prompt.ask(
        "  [k]eep / [s]kip / [q]uit",
        default="k",
        choices=["k", "s", "q"],
    )
    if action == "q":
        raise KeyboardInterrupt
    if action == "s":
        return False, default_label, default_binding, default_gate

    label = Prompt.ask("  label", default=default_label)

    binding: Optional[ParamBinding] = default_binding
    if ev.kind in ("input_change", "file_selected"):
        if binding is None:
            if Confirm.ask("  mark this value as a parameter?", default=True):
                param_name = Prompt.ask("    parameter name", default=label)
                binding = ParamBinding(
                    name=param_name,
                    type="file_path" if ev.kind == "file_selected" else "string",
                    mode="whole",
                )
        else:
            if Confirm.ask(
                f"  parameter '[cyan]{binding.name}[/cyan]'? (n to make it literal)",
                default=True,
            ):
                new_name = Prompt.ask("    parameter name", default=binding.name)
                binding.name = new_name
            else:
                binding = None

    gate = Confirm.ask(
        "  requires approval gate (irreversible)?", default=default_gate
    )
    return True, label, binding, gate


# ---- IO --------------------------------------------------------------------


def save_skill(skill: Skill, skills_dir: Path) -> Path:
    skills_dir.mkdir(parents=True, exist_ok=True)
    out = skills_dir / f"{skill.name}.json"
    out.write_text(skill.model_dump_json(indent=2), encoding="utf-8")
    return out


# ---- CLI entry -------------------------------------------------------------


def run_annotate(
    session_id: str,
    skill_name: Optional[str] = None,
    description: str = "",
    base_url: Optional[str] = None,
    portal: Optional[str] = None,
    auto: bool = False,
    sessions_dir: Path = Path("sessions"),
    skills_dir: Path = Path("skills"),
    annotate_mode: Literal["linear", "semantic"] = "semantic",
) -> Path:
    console = Console()
    session_dir = sessions_dir / session_id
    if not session_dir.exists():
        console.print(f"[red]Session dir not found:[/red] {session_dir}")
        raise SystemExit(2)

    meta = load_meta(session_dir)
    if skill_name is None:
        skill_name = meta.get("skill_name") or session_id

    raw_events = load_trace(session_dir)
    # F-05: identity + causality MUST be computed on the raw event
    # list, before filter_events drops anything. Otherwise the graph
    # is built from a pre-filtered view and downstream consumers
    # (WI-06/13/14 will replace the legacy heuristics in filter_events
    # with causality-aware logic) cannot see the dropped events'
    # causal ancestors. The graph is then passed into filter_events
    # so future WIs can read it without rebuilding.
    raw_events = _assign_synthetic_ids(list(raw_events))
    causality = build_causality_graph(raw_events)
    events = filter_events(raw_events, causality=causality)

    console.print(
        Panel.fit(
            f"[bold]Annotating session[/bold] {session_id}\n"
            f"Skill: {skill_name}\n"
            f"Events: {len(raw_events)} raw -> {len(events)} after noise filter\n"
            f"Mode:  {'auto' if auto else 'interactive'}",
            border_style="cyan",
        )
    )

    skill = build_skill(
        skill_name=skill_name,
        events=events,
        description=description,
        base_url=base_url or meta.get("base_url") or None,
        portal=portal,
        auto=auto,
        console=console,
        session_id=session_id,
        annotate_mode=annotate_mode,
    )
    out = save_skill(skill, skills_dir)

    # Summary
    table = Table(title=f"Skill '{skill.name}'", header_style="bold")
    table.add_column("#", style="dim")
    table.add_column("Action", style="cyan")
    table.add_column("Label")
    table.add_column("Param", style="yellow")
    table.add_column("Gate")
    for s in skill.steps:
        gate = "yes" if s.requires_gate else ""
        param = s.param_binding.name if s.param_binding else ""
        table.add_row(str(s.index), s.action, s.semantic_label or "", param, gate)
    console.print(table)

    console.print(
        f"\nDeclared params: "
        f"[yellow]{', '.join(p.name for p in skill.params) or '(none)'}[/yellow]"
    )
    console.print(f"Saved: [bold]{out}[/bold]")
    return out
