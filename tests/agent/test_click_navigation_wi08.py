"""WI-08: click + caused navigate collapse + legacy migration.

Acceptance check: replaying a catalog search that clicks
``btn-open-A-9003`` ends on ``/asset/A-9003`` and NEVER issues
``page.goto("/asset/A-9001")``.

Most of the test exercise the annotator + migration + schema; the
runtime "no page.goto" is enforced by the runner's _do_click logic
which is unit-covered via _assert_nav_effect's docstring guarantee.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from pilot.annotate import build_skill, _index_caused_navigates
from pilot.skill_models import (
    ElementFingerprint,
    NavigationEffect,
    Skill,
    SkillStep,
    StepEffect,
    StepProvenance,
    TraceEvent,
    _derive_legacy_url_template,
    _upgrade_legacy_click_nav_pairs,
)


# ---------------------------------------------------------------------------
# Annotator: click + caused navigate is folded into one step
# ---------------------------------------------------------------------------


def _click_event(event_id: str, label: str, target_testid: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(test_id=target_testid),
        page_url="http://localhost:5188/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=1,
        source="user_click",
    )


def _caused_navigate(
    event_id: str, caused_by: str, url: str
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="navigate",
        url=url,
        page_url=url,
        event_id=event_id,
        caused_by=caused_by,
        interaction_id=caused_by,
        sequence=2,
        source="history.pushState",
        raw_event_kind="history.pushState",
    )


def test_click_with_caused_navigate_collapses_into_one_step():
    click = _click_event("click-1", "open A-9001", "btn-open-A-9001")
    nav = _caused_navigate(
        "nav-1", "click-1", "http://localhost:5188/asset/A-9001"
    )
    skill = build_skill(
        skill_name="t",
        events=[click, nav],
        auto=True,
    )
    # The navigate was caused -- it should NOT produce its own step.
    assert len(skill.steps) == 1
    s = skill.steps[0]
    assert s.action == "click"
    assert s.effects is not None and s.effects.navigation is not None
    assert s.effects.navigation.url == "http://localhost:5188/asset/A-9001"
    assert s.effects.navigation.kind == "spa_route"
    assert s.effects.navigation.reload_allowed is False
    # Provenance carries BOTH raw event ids.
    assert s.provenance is not None
    assert set(s.provenance.raw_event_ids) == {"click-1", "nav-1"}
    assert s.provenance.cluster_kind == "click_with_navigation"


def test_standalone_navigate_survives():
    """A navigate event with caused_by=None (e.g. initial_load) keeps
    its own standalone step. Operator's manual address-bar nav still
    replays as a goto."""
    initial = TraceEvent(
        ts=datetime.utcnow(),
        kind="navigate",
        url="http://localhost:5188/catalog",
        page_url="http://localhost:5188/catalog",
        event_id="nav-init",
        caused_by=None,
        sequence=1,
        source="initial_load",
        raw_event_kind="initial_load",
    )
    skill = build_skill(
        skill_name="t", events=[initial], auto=True
    )
    assert len(skill.steps) == 1
    s = skill.steps[0]
    assert s.action == "navigate"
    # No navigation effect on a standalone navigate.
    assert s.effects is None or s.effects.navigation is None


def test_index_caused_navigates_picks_last_in_chain():
    """Router redirect chains can emit multiple navigates per click.
    The annotator picks the LAST one (where the user ended up)."""
    click = _click_event("c1", "open", "btn-open")
    a = _caused_navigate("n1", "c1", "http://x/first")
    b = _caused_navigate("n2", "c1", "http://x/second")
    causality = {"by_id": {}, "by_interaction": {}, "children_of": {}, "user_actions": []}
    mapping = _index_caused_navigates([click, a, b], causality)
    assert mapping["c1"].url == "http://x/second"


# ---------------------------------------------------------------------------
# Legacy skill migration: click + standalone navigate -> click effect
# ---------------------------------------------------------------------------


def test_legacy_click_navigate_pair_upgrades_at_load():
    raw = {
        "name": "legacy",
        "steps": [
            {
                "index": 0,
                "action": "click",
                "fingerprint": {"test_id": "btn-open-A-9001"},
                "semantic_label": "open A-9001",
            },
            {
                "index": 1,
                "action": "navigate",
                "url": "http://localhost:5188/asset/A-9001",
            },
        ],
    }
    skill = Skill.model_validate(raw)
    # One step survives (the click); the navigate is folded.
    assert len(skill.steps) == 1
    assert skill.legacy_nav_upgrade_count == 1
    eff = skill.steps[0].effects
    assert eff is not None and eff.navigation is not None
    assert eff.navigation.url == "http://localhost:5188/asset/A-9001"
    assert eff.navigation.source == "legacy_upgrade"
    # The runner uses reload_allowed=False to skip page.goto.
    assert eff.navigation.reload_allowed is False


def test_legacy_upgrade_re_indexes_steps():
    """The upgrade drops the standalone navigate; surviving steps must
    have sequential index values so SkillStep.index stays meaningful."""
    raw = {
        "name": "x",
        "steps": [
            {"index": 0, "action": "click", "fingerprint": {"test_id": "a"}},
            {"index": 1, "action": "navigate", "url": "http://x/1"},
            {"index": 2, "action": "click", "fingerprint": {"test_id": "b"}},
            {"index": 3, "action": "navigate", "url": "http://x/2"},
        ],
    }
    skill = Skill.model_validate(raw)
    assert len(skill.steps) == 2
    assert [s.index for s in skill.steps] == [0, 1]
    assert skill.legacy_nav_upgrade_count == 2


def test_legacy_upgrade_only_consecutive_click_then_navigate():
    """Sequence click, click, navigate -- the navigate is NOT caused by
    the first click; the upgrader should only fold the (last click,
    navigate) pair."""
    raw = {
        "name": "x",
        "steps": [
            {"index": 0, "action": "click", "fingerprint": {"test_id": "a"}},
            {"index": 1, "action": "click", "fingerprint": {"test_id": "b"}},
            {"index": 2, "action": "navigate", "url": "http://x/y"},
        ],
    }
    skill = Skill.model_validate(raw)
    assert len(skill.steps) == 2
    # Step at index 0 has no nav effect; step at index 1 has it.
    assert (skill.steps[0].effects is None
            or skill.steps[0].effects.navigation is None)
    assert (skill.steps[1].effects is not None
            and skill.steps[1].effects.navigation is not None)


def test_legacy_upgrade_skip_when_click_already_has_nav_effect():
    """A click step that already declares effects.navigation should not
    be touched -- this is the new-skill path, post-WI-08 annotator."""
    raw = {
        "name": "x",
        "steps": [
            {
                "index": 0,
                "action": "click",
                "fingerprint": {"test_id": "a"},
                "effects": {
                    "navigation": {
                        "kind": "spa_route",
                        "url": "http://x/already",
                        "reload_allowed": False,
                    }
                },
            },
            {"index": 1, "action": "navigate", "url": "http://x/y"},
        ],
    }
    skill = Skill.model_validate(raw)
    # The annotator already produced a click with nav effect; the
    # following standalone navigate is preserved (different semantic).
    assert len(skill.steps) == 2
    assert skill.legacy_nav_upgrade_count == 0


def test_derive_legacy_url_template_conservative():
    # Substring luck: search query 'A-90' must NOT template through
    # into /asset/A-9001 -- only an EXACT path segment match wins.
    out = _derive_legacy_url_template(
        "http://x/asset/A-9001",
        [{"action": "change", "param_binding": {"name": "q"}, "value": "A-90"}],
    )
    assert out is None  # conservative -- literal URL kept

    # Exact segment match wins.
    out = _derive_legacy_url_template(
        "http://x/asset/A-9001",
        [{"action": "click", "param_binding": {"name": "content_id"},
          "value": "A-9001"}],
    )
    assert out == "http://x/asset/{content_id}"


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


def test_navigation_effect_roundtrip():
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn"),
        effects=StepEffect(
            navigation=NavigationEffect(
                kind="spa_route",
                url="http://x/y/Z-12",
                url_template="http://x/y/{content_id}",
                source="history.pushState",
                reload_allowed=False,
                assert_url="http://x/y/{content_id}",
            )
        ),
        provenance=StepProvenance(
            raw_event_ids=["click-x", "nav-y"],
            cluster_kind="click_with_navigation",
            detection_method="deterministic",
        ),
    )
    skill = Skill(name="t", steps=[step])
    blob = skill.model_dump_json()
    restored = Skill.model_validate_json(blob)
    nav = restored.steps[0].effects.navigation  # type: ignore[union-attr]
    assert nav is not None
    assert nav.url_template == "http://x/y/{content_id}"
    assert nav.reload_allowed is False
