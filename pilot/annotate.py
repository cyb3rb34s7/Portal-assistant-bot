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
    DomExpectation,
    ExpectedSignals,
    NavigationEffect,
    ParamBinding,
    SemanticCluster,
    Skill,
    SkillParam,
    SkillStep,
    StepEffect,
    StepProvenance,
    TraceEvent,
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

        # Drop input_change with empty value immediately after an empty state
        if ev.kind == "input_change" and (ev.value is None or ev.value == ""):
            if prev and prev.kind == "input_change" and prev.fingerprint and ev.fingerprint:
                if _same_target(prev.fingerprint, ev.fingerprint):
                    continue

        # Drop click that is immediately followed by the same click (<200ms)
        if (
            ev.kind == "click"
            and prev
            and prev.kind == "click"
            and prev.fingerprint
            and ev.fingerprint
            and _same_target(prev.fingerprint, ev.fingerprint)
            and (ev.ts - prev.ts).total_seconds() < 0.2
        ):
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


# ---- WI-12: semantic clustering pipeline -----------------------------------


# Observed events that contribute to a cluster's audit trail but are not
# themselves user-facing steps. These get folded into the owning cluster's
# raw_event_ids (so the audit log shows "this fill_submit step came from
# input + input + submit + 2 network responses") but never produce
# standalone clusters / steps.
_OBSERVED_EVENT_KINDS: frozenset[str] = frozenset({
    "dom_mutation", "network_request", "network_response",
    "popup", "download", "visibility_change",
})


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

    clusters: list[SemanticCluster] = []
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

    steps: list[SkillStep] = []
    declared_params: dict[str, SkillParam] = {}
    skipped = 0
    # Track params-seen so navigation URL templates can substitute
    # values that appear in the URL (WI-08 + foundation for WI-11).
    params_seen: list[tuple[str, str]] = []

    # F-07/WI-10: observed-event kinds (dom_mutation, network_request,
    # network_response, popup, download, visibility_change) feed the
    # annotator's structural derivation -- they are NOT user-facing
    # steps themselves. Skip them in the per-event loop so the skill
    # only carries semantic steps.
    _OBSERVED_KINDS: frozenset[str] = frozenset({
        "dom_mutation", "network_request", "network_response",
        "popup", "download", "visibility_change",
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

        label = auto_label(ev)
        action = action_for_kind(ev.kind)
        binding = infer_param_binding(ev, label)
        gate = infer_gate(label, ev)

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

        # WI-10: collect readiness signals attributed to this user
        # event so the step's expected_signals.dom gets populated.
        expected_signals: Optional[ExpectedSignals] = None
        if ev.event_id and ev.event_id in readiness_by_cause:
            expected_signals = ExpectedSignals(
                dom=list(readiness_by_cause[ev.event_id])
            )

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
        step = SkillStep(
            index=len(steps),
            action=action,
            fingerprint=ev.fingerprint,
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
                final_type = "file_path"
                final_codec = "file_ref"
            else:
                final_type = inferred_type
                final_codec = inferred_codec
            declared_params[binding.name] = SkillParam(
                name=binding.name,
                type=final_type,  # type: ignore[arg-type]
                codec=final_codec,  # type: ignore[arg-type]
                description=f"Value for step {step.index}: {label}",
                example=example or None,
                required=True,
                enum_options=(
                    list(fp.options_snapshot)
                    if fp and fp.options_snapshot
                    else None
                ),
            )

    if skipped and not auto:
        console.print(f"[dim]Skipped {skipped} event(s) marked as noise.[/dim]")

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
