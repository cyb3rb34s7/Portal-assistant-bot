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
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .param_codecs import infer_param_type_and_codec
from .skill_models import (
    ActionType,
    NavigationEffect,
    ParamBinding,
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


def _derive_url_template(
    nav_url: Optional[str],
    params_seen: list[tuple[str, str]],
) -> Optional[str]:
    """Derive a ``url_template`` for a click's navigation effect.

    Conservative rule: only template when a recorded param value is
    the WHOLE final URL path segment. This rejects the substring-luck
    pattern that the audit flagged (search query 'A-90' becoming part
    of '/asset/A-9001' as '/asset/{q}01'). True provenance-based
    templating ships in WI-11; until then a literal URL is safer than
    a wrong template.

    Returns the templated string, or None if no substitution applied
    (we keep the literal URL in that case)."""
    if not nav_url:
        return None
    try:
        path = nav_url.split("?", 1)[0].split("#", 1)[0]
        last_seg = path.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        return None
    if not last_seg or len(last_seg) < 3:
        return None
    for name, value in params_seen:
        if value == last_seg:
            return nav_url.replace(last_seg, "{" + name + "}", 1)
    return None


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

    steps: list[SkillStep] = []
    declared_params: dict[str, SkillParam] = {}
    skipped = 0
    # Track params-seen so navigation URL templates can substitute
    # values that appear in the URL (WI-08 + foundation for WI-11).
    params_seen: list[tuple[str, str]] = []

    for idx, ev in enumerate(events):
        # WI-08: skip caused-navigate events. They get folded into
        # their causing click below.
        if (
            ev.kind == "navigate"
            and ev.event_id
            and ev.event_id in folded_nav_ids
        ):
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
            url_template = _derive_url_template(nav_url, params_seen)
            # Derive a route-only assert from the URL template / url.
            assert_url = url_template if url_template else None
            effects = StepEffect(
                navigation=NavigationEffect(
                    kind=nav_kind,  # type: ignore[arg-type]
                    url=nav_url,
                    url_template=url_template,
                    source=folded_nav.source or folded_nav.raw_event_kind,
                    reload_allowed=False,
                    assert_url=assert_url,
                )
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
            # WI-02 + WI-08: link the step back to its source raw event.
            # When a navigate is folded, BOTH the click and the navigate
            # event ids become raw_event_ids so the audit trail shows
            # which raw events the semantic step came from.
            provenance=StepProvenance(
                raw_event_ids=(
                    [ev.event_id, folded_nav.event_id]  # type: ignore[list-item]
                    if ev.event_id and folded_nav and folded_nav.event_id
                    else ([ev.event_id] if ev.event_id else [])
                ),
                cluster_kind=(
                    "click_with_navigation"
                    if folded_nav is not None
                    else "single_event"
                ),
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
    )
    _derive_fingerprint_templates(skill)
    return skill


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

    # Fields we'll scan for substring matches. Keep this list in sync
    # with skill_runner._materialize_fingerprint.
    scan_fields = (
        "test_id",
        "element_id",
        "css_path",
        "xpath",
        "accessible_name",
        "text",
    )

    templated_count = 0
    for s in skill.steps:
        fp = s.fingerprint
        if fp is None:
            continue
        for field in scan_fields:
            literal = getattr(fp, field, None)
            if not literal or not isinstance(literal, str):
                continue
            template = literal
            for param_name, value in unique:
                if value in template:
                    template = template.replace(
                        value, "{" + param_name + "}"
                    )
            if template != literal:
                fp.templates[field] = template
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
