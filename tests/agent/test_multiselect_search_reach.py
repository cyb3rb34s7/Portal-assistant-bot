"""Real-portal multi-select-with-search record + replay fix.

Operator feedback from a real OTT "Migrate framework" page: multi-select
dropdowns (Year / Target Model / Filter / Country) failed at replay in two
ways:

  Case A: operator clicked an option that was visible; at replay the target
          is a DIFFERENT option that's off-viewport -> unreachable.
  Case B: operator typed into the (server-side) search box then clicked;
          replay filled the search with the item id, not its label, so a
          label-indexed server search surfaced nothing.

Locked design (see DOCS/reviews/2026-05-22 report):
  - Reaching an option is replay-time logic, not replayed keystrokes.
  - Always prefer search-to-narrow when the picker has a search box; type
    the option's LABEL; wait for the target checkbox to become visible
    (rides out a server-side spinner, no fixed sleep); click; clear search.
  - Fallback (no search box): locator + scroll_into_view_if_needed (lists
    are non-virtualized, so any rendered option is reachable).

These tests pin the data contract (schema + annotator) and the runner's
reach behaviour (label-not-id, visibility-gated, search fallback).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_set_selection_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    OptionSnapshot,
    SetSelectionSpec,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import SkillRunner


# ---- event helpers --------------------------------------------------------


def _click(event_id, test_id, *, accessible_name=None, sequence=1):
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="input" if "checkbox" in test_id else "button",
            role="checkbox" if "checkbox" in test_id else "button",
            accessible_name=accessible_name,
        ),
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _click_with_options(event_id, test_id, options_seen, *,
                        accessible_name=None, sequence=1):
    ev = _click(
        event_id, test_id, accessible_name=accessible_name, sequence=sequence
    )
    ev.options_seen = [
        OptionSnapshot(value=v, label=lbl) for (v, lbl) in options_seen
    ]
    return ev


def _search(event_id, test_id, value, sequence=1, options_seen=None):
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(test_id=test_id, tag="input"),
        value=value,
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        sequence=sequence,
        source="user_input",
    )
    if options_seen is not None:
        ev.options_seen = [
            OptionSnapshot(value=v, label=lbl) for (v, lbl) in options_seen
        ]
    return ev


# ---- schema ---------------------------------------------------------------


def test_set_selection_spec_new_fields_roundtrip() -> None:
    spec = SetSelectionSpec(
        mode="replace",
        param="country",
        item_labels={"zw": "Zimbabwe", "us": "United States"},
        select_strategy="search",
        option_list_selector="[data-testid='multiselect-country-popover']",
    )
    step = SkillStep(
        index=0,
        action="set_selection",
        fingerprint=ElementFingerprint(test_id="multiselect-country-toggle"),
        set_selection=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0].set_selection
    assert rs.item_labels == {"zw": "Zimbabwe", "us": "United States"}
    assert rs.select_strategy == "search"
    assert rs.option_list_selector == "[data-testid='multiselect-country-popover']"


def test_set_selection_spec_defaults_preserve_legacy() -> None:
    """A spec built the old way (no new fields) defaults to direct
    strategy + empty labels, so existing skills are unchanged."""
    spec = SetSelectionSpec(mode="replace", param="categories")
    assert spec.select_strategy == "direct"
    assert spec.item_labels == {}
    assert spec.option_list_selector is None


# ---- annotator: strategy + labels -----------------------------------------


def test_annotator_search_box_sets_search_strategy_and_labels() -> None:
    """Case B: a cluster containing a search event -> select_strategy
    'search', and item_labels carries each option's display label
    (from accessible_name) so the runner types the LABEL not the id."""
    events = _assign_synthetic_ids([
        _click("c1", "multiselect-country-toggle", sequence=1),
        _search("i1", "multiselect-country-search", "zim", sequence=2),
        _click(
            "c2", "multiselect-country-checkbox-zw",
            accessible_name="Zimbabwe", sequence=3,
        ),
        _click("c3", "multiselect-country-toggle", sequence=4),
    ])
    causality = build_causality_graph(events)
    clusters = _detect_set_selection_clusters(events, causality, set())
    assert len(clusters) == 1

    skill = build_skill(skill_name="set_country", events=events, auto=True)
    spec = next(
        s.set_selection for s in skill.steps if s.action == "set_selection"
    )
    assert spec.select_strategy == "search"
    assert spec.item_labels.get("zw") == "Zimbabwe"


def test_annotator_no_search_box_is_direct_strategy() -> None:
    """Case A: no search event in the cluster -> direct strategy. Labels
    are still captured so a fallback search (if the picker turns out to
    have a box) can still type the label."""
    events = _assign_synthetic_ids([
        _click("c1", "multiselect-country-toggle", sequence=1),
        _click(
            "c2", "multiselect-country-checkbox-us",
            accessible_name="United States", sequence=2,
        ),
        _click("c3", "multiselect-country-toggle", sequence=3),
    ])
    skill = build_skill(skill_name="set_country", events=events, auto=True)
    spec = next(
        s.set_selection for s in skill.steps if s.action == "set_selection"
    )
    assert spec.select_strategy == "direct"
    assert spec.item_labels.get("us") == "United States"


# ---- annotator: id+label sprint (known_options + stray-param fix) --------


def test_annotator_no_stray_param_for_set_selection() -> None:
    """Finding 3a regression: a set_selection cluster must declare ONLY
    its list param. The checkbox-click (the cluster's primary target)
    must NOT also be bound as its own boolean param
    (multiselect_country_checkbox_ar), and the step must be labeled as a
    set_selection step (not fill_multiselect_country_checkbox_ar)."""
    events = _assign_synthetic_ids([
        _click("c1", "multiselect-country-toggle", sequence=1),
        _search("i1", "multiselect-country-search", "Argentina", sequence=2),
        _click(
            "c2", "multiselect-country-checkbox-ar",
            accessible_name="Argentina", sequence=3,
        ),
        _click("c3", "multiselect-country-toggle", sequence=4),
    ])
    skill = build_skill(skill_name="set_country", events=events, auto=True)

    # Exactly one set_selection step.
    ss_steps = [s for s in skill.steps if s.action == "set_selection"]
    assert len(ss_steps) == 1
    step = ss_steps[0]

    # No stray boolean param, only the list param 'country'.
    param_names = {p.name for p in skill.params}
    assert "multiselect_country_checkbox_ar" not in param_names
    assert param_names == {"country"}
    country = next(p for p in skill.params if p.name == "country")
    assert country.type == "string_list"

    # The step carries no stray binding and a clean label.
    assert step.param_binding is None
    assert "checkbox" not in (step.semantic_label or "")
    assert step.semantic_label == "set_selection_multiselect_country"


def test_annotator_populates_known_options_and_label_example() -> None:
    """id+label sprint: known_options is the full universe seen (unioned
    from every event's options_seen), the param example is the selected
    LABEL (not the id), and enum_options carries the label universe."""
    events = _assign_synthetic_ids([
        _click(
            "c1", "multiselect-country-toggle",
            sequence=1,
        ),
        # Search "a" surfaces several countries.
        _search(
            "i1", "multiselect-country-search", "a", sequence=2,
            options_seen=[
                ("ar", "Argentina"),
                ("au", "Australia"),
                ("at", "Austria"),
            ],
        ),
        # Operator narrows + clicks Argentina; the click event also
        # carries the currently-rendered universe.
        _click_with_options(
            "c2", "multiselect-country-checkbox-ar",
            [("ar", "Argentina")],
            accessible_name="Argentina", sequence=3,
        ),
        _click("c3", "multiselect-country-toggle", sequence=4),
    ])
    skill = build_skill(skill_name="set_country", events=events, auto=True)
    spec = next(
        s.set_selection for s in skill.steps if s.action == "set_selection"
    )

    # known_options is the SUPERSET of everything seen.
    ko = {opt.value: opt.label for opt in spec.known_options}
    assert ko == {
        "ar": "Argentina",
        "au": "Australia",
        "at": "Austria",
    }
    # item_labels is only the SELECTED subset.
    assert spec.item_labels == {"ar": "Argentina"}

    # The declared param example + enum read LABELS, not ids.
    country = next(p for p in skill.params if p.name == "country")
    assert country.example == "Argentina"  # selected label, not "ar"
    enum_labels = {o.label for o in (country.enum_options or [])}
    assert enum_labels == {"Argentina", "Australia", "Austria"}


# ---- runner: reach behaviour ----------------------------------------------


class _FakeLocator:
    """Records fill/click/scroll/wait_for calls so a test can assert the
    runner typed the LABEL (not the id) and gated on visibility."""

    def __init__(self, log, name):
        self._log = log
        self._name = name

    def fill(self, value, **_kw):
        self._log.append(("fill", self._name, value))

    def click(self, **_kw):
        self._log.append(("click", self._name))

    def scroll_into_view_if_needed(self, **_kw):
        self._log.append(("scroll", self._name))

    def wait_for(self, **kw):
        self._log.append(("wait_for", self._name, kw.get("state")))

    def evaluate(self, _expr):
        # _robust_click's aria probe. Report an aria-checked flip so the
        # real click is treated as having had an effect (no fallback) --
        # these tests assert the reach logic, not the click fallback.
        self._log.append(("evaluate", self._name))
        self._aria_calls = getattr(self, "_aria_calls", 0) + 1
        return f"aria-checked={'true' if self._aria_calls > 1 else 'false'}"

    def dispatch_event(self, event, **_kw):
        self._log.append(("dispatch_event", self._name, event))


class _FakePage:
    """Minimal stand-in for the Playwright Page the runner reads via
    ``self.session.page``. The reach helpers only need ``page`` to exist;
    locator resolution is stubbed via ``_locate_via_template``."""

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, _expr):
        return 0


class _FakeSelectionPage(_FakePage):
    """Fake page that returns scripted current/final selection reads."""

    def __init__(self, selection_reads):
        self._selection_reads = list(selection_reads)

    def evaluate(self, _expr, *args):
        if args:
            return self._selection_reads.pop(0)
        return 0


class _FakeSession:
    def __init__(self, page=None):
        self.page = page or _FakePage()


def _make_runner() -> SkillRunner:
    skill = Skill(
        name="t",
        steps=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    runner = SkillRunner(
        session=None,  # type: ignore[arg-type]
        skill=skill,
        params={},
        sessions_dir=Path(tempfile.gettempdir()),
    )
    runner.session = _FakeSession()  # type: ignore[assignment]
    return runner


def test_runner_search_path_types_label_and_clicks_row_by_label() -> None:
    """The crux of Case B + id+label locked decision #3: when
    select_strategy='search', the runner fills the search box with the
    option's LABEL ('Zimbabwe'), never the id ('zw'), waits for the
    surfaced ROW (matched by visible label, NOT the id template) to be
    visible, and clicks THAT row -- so a replay-time-new target reaches
    without an id template."""
    runner = _make_runner()
    log: list = []

    spec = SetSelectionSpec(
        mode="replace",
        param="country",
        search_fp=ElementFingerprint(test_id="multiselect-country-search"),
        checkbox_template_fp=ElementFingerprint(
            test_id="multiselect-country-checkbox-{item}"
        ),
        item_labels={"zw": "Zimbabwe"},
        select_strategy="search",
        option_list_selector="[data-testid='multiselect-country-popover']",
    )

    def _fake_locate(fp, params):
        if fp is spec.search_fp:
            return _FakeLocator(log, "search")
        # The id template must NOT be used for the click on the search
        # path; if it is, the test catches it as a 'checkbox-...' click.
        return _FakeLocator(log, f"checkbox-{params.get('item')}")

    runner._locate_via_template = _fake_locate  # type: ignore[assignment]
    runner._set_selection_dom_timeout_ms = lambda: 1000  # type: ignore[assignment]
    # The reach-by-label-row resolution is stubbed: it returns a fake
    # locator named by the label it was asked to find, so we can assert
    # the click targeted the row (by label) and never the id template.
    runner._locate_option_row_by_label = (  # type: ignore[assignment]
        lambda _spec, lbl: _FakeLocator(log, f"row[{lbl}]")
    )

    result = runner._search_then_click_option(spec, "zw", "Zimbabwe")
    assert result is True

    fills = [e for e in log if e[0] == "fill" and e[1] == "search"]
    # First fill is the LABEL, not the id.
    assert fills[0] == ("fill", "search", "Zimbabwe")
    assert ("fill", "search", "zw") not in log
    # Visibility-gated on the row, then clicked -- by LABEL, not id.
    assert ("wait_for", "row[Zimbabwe]", "visible") in log
    assert ("click", "row[Zimbabwe]") in log
    # The id-templated checkbox must NOT be clicked on the search path.
    assert ("click", "checkbox-zw") not in log
    # Search cleared afterward for the next item.
    assert fills[-1] == ("fill", "search", "")


def test_runner_set_selection_param_item_can_carry_label_for_new_id() -> None:
    """id+label locked decision #2: the operator passes a LABEL
    (``country=Zimbabwe``); the runner resolves label->id via
    known_options for the equality assertion, searches the label, and
    clicks the surfaced ROW by label (never the id template). The new
    target ('Zimbabwe'->'zw') was NOT the recorded selection
    ('Argentina')."""
    runner = _make_runner()
    runner.params = {"country": ["Zimbabwe"]}
    # current selection read=[], final read after click=["zw"].
    runner.session = _FakeSession(_FakeSelectionPage([[], ["zw"]]))  # type: ignore[assignment]
    log: list = []

    spec = SetSelectionSpec(
        mode="replace",
        param="country",
        search_fp=ElementFingerprint(test_id="multiselect-country-search"),
        checkbox_template_fp=ElementFingerprint(
            test_id="multiselect-country-checkbox-{item}"
        ),
        current_items_selector="[data-testid^='multiselect-country-chip-']",
        current_items_id_attr="data-testid",
        current_items_id_prefix="multiselect-country-chip-",
        item_labels={"ar": "Argentina"},
        # The universe seen at record carried both ar/Argentina and the
        # later-surfaced zw/Zimbabwe so label->id resolution works.
        known_options=[
            OptionSnapshot(value="ar", label="Argentina"),
            OptionSnapshot(value="zw", label="Zimbabwe"),
        ],
        select_strategy="search",
        option_list_selector="[data-testid='multiselect-country-popover']",
    )
    step = SkillStep(
        index=0,
        action="set_selection",
        fingerprint=ElementFingerprint(test_id="multiselect-country-toggle"),
        set_selection=spec,
    )

    def _fake_locate(fp, params):
        if fp is spec.search_fp:
            return _FakeLocator(log, "search")
        return _FakeLocator(log, f"checkbox-{params.get('item')}")

    runner._locate_via_template = _fake_locate  # type: ignore[assignment]
    runner._set_selection_dom_timeout_ms = lambda: 1000  # type: ignore[assignment]
    runner._locate_option_row_by_label = (  # type: ignore[assignment]
        lambda _spec, lbl: _FakeLocator(log, f"row[{lbl}]")
    )

    result, level = runner._do_set_selection(step)

    assert result.success is True
    assert level == 1
    fills = [e for e in log if e[0] == "fill" and e[1] == "search"]
    # Searched by the resolved LABEL, never the id.
    assert fills[0] == ("fill", "search", "Zimbabwe")
    assert ("fill", "search", "zw") not in log
    # Reached + clicked the ROW by label; id template never clicked.
    assert ("wait_for", "row[Zimbabwe]", "visible") in log
    assert ("click", "row[Zimbabwe]") in log
    assert ("click", "checkbox-zw") not in log
    assert fills[-1] == ("fill", "search", "")


def test_runner_unknown_label_warns_but_still_reaches() -> None:
    """id+label sprint: a target label not in known_options is not fatal
    -- the runner emits set_selection_unknown_label (warn) and still
    attempts the search-by-label reach. Equality is label-aware: the
    pseudo-id (the label) matches the chip via the label map."""
    runner = _make_runner()
    # 'Atlantis' is NOT in known_options -> unknown label path. The
    # portal still surfaces a chip whose id is the label string here.
    runner.params = {"country": ["Atlantis"]}
    runner.session = _FakeSession(  # type: ignore[assignment]
        _FakeSelectionPage([[], ["Atlantis"]])
    )
    log: list = []
    diags: list = []
    runner._diagnostic = (  # type: ignore[assignment]
        lambda code, **kw: diags.append((code, kw))
    )

    spec = SetSelectionSpec(
        mode="replace",
        param="country",
        search_fp=ElementFingerprint(test_id="multiselect-country-search"),
        checkbox_template_fp=ElementFingerprint(
            test_id="multiselect-country-checkbox-{item}"
        ),
        current_items_selector="[data-testid^='multiselect-country-chip-']",
        current_items_id_attr="data-testid",
        current_items_id_prefix="multiselect-country-chip-",
        known_options=[
            OptionSnapshot(value="zw", label="Zimbabwe"),
        ],
        select_strategy="search",
        option_list_selector="[data-testid='multiselect-country-popover']",
    )
    step = SkillStep(
        index=0,
        action="set_selection",
        fingerprint=ElementFingerprint(test_id="multiselect-country-toggle"),
        set_selection=spec,
    )
    runner._locate_via_template = (  # type: ignore[assignment]
        lambda fp, params: _FakeLocator(log, "search")
    )
    runner._set_selection_dom_timeout_ms = lambda: 1000  # type: ignore[assignment]
    runner._locate_option_row_by_label = (  # type: ignore[assignment]
        lambda _spec, lbl: _FakeLocator(log, f"row[{lbl}]")
    )

    result, level = runner._do_set_selection(step)

    assert result.success is True
    assert level == 1
    # Warned about the unknown label.
    assert any(c == "runner.set_selection_unknown_label" for c, _ in diags)
    # Still searched + clicked by the label.
    assert ("fill", "search", "Atlantis") in log
    assert ("click", "row[Atlantis]") in log


def test_runner_direct_path_scrolls_before_click() -> None:
    """Case A fallback: direct strategy resolves the checkbox template,
    scrolls it into view, then clicks -- so an off-viewport (but
    rendered) option is reachable without search."""
    runner = _make_runner()
    log: list = []

    spec = SetSelectionSpec(
        mode="replace",
        param="country",
        checkbox_template_fp=ElementFingerprint(
            test_id="multiselect-country-checkbox-{item}"
        ),
        item_labels={"us": "United States"},
        select_strategy="direct",
    )
    runner._locate_via_template = (  # type: ignore[assignment]
        lambda fp, params: _FakeLocator(log, f"checkbox-{params.get('item')}")
    )

    err = runner._reach_and_click_option(spec, "us")
    assert err is None
    assert ("scroll", "checkbox-us") in log
    assert ("click", "checkbox-us") in log
    # scroll precedes click.
    assert log.index(("scroll", "checkbox-us")) < log.index(("click", "checkbox-us"))


# ---- runner: _robust_click effect-gated dispatch_event fallback -----------


class _RobustLocator:
    """Locator that records click()/dispatch_event() and reports a
    scripted aria-snapshot sequence so the effect probe can be exercised.

    ``aria_values`` is a list consumed one entry per ``evaluate`` call:
    None means the element has no aria-* attr (probe falls back to the
    mutation watcher); a string like 'aria-expanded=false' is a snapshot.
    """

    def __init__(self, log, *, aria_values):
        self._log = log
        self._aria = list(aria_values)

    def click(self, **_kw):
        self._log.append(("click",))

    def dispatch_event(self, event, **_kw):
        self._log.append(("dispatch_event", event))

    def evaluate(self, _expr):
        return self._aria.pop(0) if self._aria else None


class _RobustPage:
    """Page whose __cp_last_mutation_at can be scripted via ``mutation``;
    each evaluate() call returns the next value (so the test can simulate
    'mutation advanced' vs 'no mutation')."""

    def __init__(self, *, mutation_values=None):
        self._mut = list(mutation_values or [])

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, _expr):
        return self._mut.pop(0) if self._mut else 0


class _RobustSession:
    def __init__(self, page):
        self.page = page


def _robust_runner(page) -> SkillRunner:
    runner = _make_runner()
    runner.session = _RobustSession(page)  # type: ignore[assignment]
    runner._ensure_watchers = lambda _p: None  # type: ignore[assignment]
    return runner


def test_robust_click_aria_change_no_fallback() -> None:
    """When the aria probe flips (real click had an effect), the runner
    clicks once and does NOT dispatch a fallback event."""
    log: list = []
    page = _RobustPage()
    runner = _robust_runner(page)
    loc = _RobustLocator(
        log,
        aria_values=["aria-expanded=false", "aria-expanded=true"],
    )
    runner._robust_click(loc)
    assert ("click",) in log
    assert not any(e[0] == "dispatch_event" for e in log)


def test_robust_click_aria_unchanged_triggers_dispatch() -> None:
    """The CDP no-op case: aria-expanded stays false after the real
    click, so the runner falls back to dispatch_event('click') and emits
    the diagnostic."""
    log: list = []
    page = _RobustPage()
    runner = _robust_runner(page)
    loc = _RobustLocator(
        log,
        aria_values=["aria-expanded=false", "aria-expanded=false"],
    )
    runner._robust_click(loc)
    # click() fired first, THEN dispatch_event as the fallback.
    assert log[0] == ("click",)
    assert ("dispatch_event", "click") in log
    assert log.index(("click",)) < log.index(("dispatch_event", "click"))
    codes = [d.code for d in runner.diagnostics]
    assert "runner.click_fallback_dispatch" in codes


def test_robust_click_mutation_probe_no_mutation_falls_back() -> None:
    """No aria attr -> document-level mutation probe. If
    __cp_last_mutation_at did not advance after the click, treat as
    no-effect and dispatch."""
    log: list = []
    # evaluate sequence: before=1000 (mutation snapshot), after=1000 (unchanged)
    page = _RobustPage(mutation_values=[1000, 1000])
    runner = _robust_runner(page)
    loc = _RobustLocator(log, aria_values=[None])  # no aria attr
    runner._robust_click(loc)
    assert log[0] == ("click",)
    assert ("dispatch_event", "click") in log


def test_robust_click_mutation_probe_advanced_no_fallback() -> None:
    """No aria attr but the mutation timestamp advanced after the click
    -> the click had an effect, no fallback."""
    log: list = []
    page = _RobustPage(mutation_values=[1000, 1500])
    runner = _robust_runner(page)
    loc = _RobustLocator(log, aria_values=[None])
    runner._robust_click(loc)
    assert ("click",) in log
    assert not any(e[0] == "dispatch_event" for e in log)
