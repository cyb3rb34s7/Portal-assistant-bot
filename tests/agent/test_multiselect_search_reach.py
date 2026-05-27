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


def _search(event_id, test_id, value, sequence=1):
    return TraceEvent(
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


class _FakeSession:
    def __init__(self):
        self.page = _FakePage()


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


def test_runner_search_path_types_label_not_id() -> None:
    """The crux of Case B: when select_strategy='search', the runner fills
    the search box with the option's LABEL ('Zimbabwe'), never the id
    ('zw'), and waits for the target checkbox to be visible before
    clicking."""
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
    )

    def _fake_locate(fp, params):
        if fp is spec.search_fp:
            return _FakeLocator(log, "search")
        return _FakeLocator(log, f"checkbox-{params.get('item')}")

    runner._locate_via_template = _fake_locate  # type: ignore[assignment]
    runner._set_selection_dom_timeout_ms = lambda: 1000  # type: ignore[assignment]

    result = runner._search_then_click_option(spec, "zw", "Zimbabwe")
    assert result is True

    fills = [e for e in log if e[0] == "fill" and e[1] == "search"]
    # First fill is the LABEL, not the id.
    assert fills[0] == ("fill", "search", "Zimbabwe")
    assert ("fill", "search", "zw") not in log
    # Visibility-gated before click.
    assert ("wait_for", "checkbox-zw", "visible") in log
    assert ("click", "checkbox-zw") in log
    # Search cleared afterward for the next item.
    assert fills[-1] == ("fill", "search", "")


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
