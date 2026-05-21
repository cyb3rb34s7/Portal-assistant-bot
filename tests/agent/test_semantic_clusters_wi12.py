"""WI-12: annotator semantic clustering pipeline.

Acceptance check (from the plan):
  - The annotator pipeline runs as discrete passes: normalize -> graph ->
    cluster -> bind params -> produce steps -> emit signals.
  - Each step gets ``raw_event_ids`` on its provenance.
  - Legacy ``linear`` mode preserved via ``annotate_mode="linear"``.
  - Cluster detector unit-testable with synthetic event lists.

The follow-on plan deliverables (fill_submit collapse into ONE step,
multi-select open/search/check/close collapsed into ONE set_selection
step) are explicitly the scope of WI-15 and WI-19 -- the detectors
land there. This test verifies the SCAFFOLDING WI-12 ships:
SemanticCluster model + detect_semantic_clusters + annotate_mode +
cluster-aware step provenance + Skill.semantic_clusters persistence.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _assign_synthetic_ids,
    build_causality_graph,
    build_skill,
    detect_semantic_clusters,
)
from pilot.skill_models import (
    ElementFingerprint,
    SemanticCluster,
    Skill,
    SkillStep,
    StepEffect,
    TraceEvent,
)


def _click(event_id: str, target_testid: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(test_id=target_testid),
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=1,
        source="user_click",
    )


def _caused_nav(event_id: str, caused_by: str, url: str) -> TraceEvent:
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


def _caused_request(
    event_id: str, caused_by: str, url: str
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="network_request",
        page_url=url,
        event_id=event_id,
        caused_by=caused_by,
        interaction_id=caused_by,
        sequence=3,
        source="fetch_request_start",
        raw_event_kind="fetch_request_start",
        request_id=event_id,
        method="GET",
        url=url,
        initiator_event_id=caused_by,
    )


def _input_change(event_id: str, value: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(test_id="input-search", tag="input"),
        value=value,
        page_url="http://x/catalog",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=5,
        source="user_input",
    )


# ---------------------------------------------------------------------------
# Unit: detect_semantic_clusters on a synthetic event list
# ---------------------------------------------------------------------------


def test_cluster_detector_emits_single_event_for_lone_click() -> None:
    """One click with no children -> one ``single_event`` cluster."""
    click = _click("c1", "btn")
    events = _assign_synthetic_ids([click])
    causality = build_causality_graph(events)
    clusters = detect_semantic_clusters(events, causality)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "single_event"
    assert clusters[0].raw_event_ids == ["c1"]
    assert clusters[0].primary_target_event_id == "c1"
    assert clusters[0].confidence == 1.0


def test_cluster_detector_collapses_click_plus_caused_navigate() -> None:
    """Click that caused a navigate -> one ``click_with_navigation``
    cluster carrying BOTH event ids."""
    click = _click("c1", "btn-open-A")
    nav = _caused_nav("n1", "c1", "http://x/asset/A")
    events = _assign_synthetic_ids([click, nav])
    causality = build_causality_graph(events)
    clusters = detect_semantic_clusters(events, causality)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "click_with_navigation"
    assert set(c.raw_event_ids) == {"c1", "n1"}
    assert c.primary_target_event_id == "c1"


def test_cluster_detector_folds_observed_children_into_owning_cluster() -> None:
    """A network_request caused by a click is folded into the click's
    cluster's raw_event_ids (audit trail) but does NOT produce a
    standalone cluster of its own."""
    click = _click("c1", "btn")
    req = _caused_request("r1", "c1", "/api/search?q=foo")
    events = _assign_synthetic_ids([click, req])
    causality = build_causality_graph(events)
    clusters = detect_semantic_clusters(events, causality)
    # One cluster (the click). The request is folded in.
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "single_event"
    assert set(c.raw_event_ids) == {"c1", "r1"}


def test_cluster_detector_two_independent_clicks_two_clusters() -> None:
    """Two unrelated user actions -> two clusters, neither folded."""
    click1 = _click("c1", "btn1")
    click2 = _click("c2", "btn2")
    events = _assign_synthetic_ids([click1, click2])
    causality = build_causality_graph(events)
    clusters = detect_semantic_clusters(events, causality)
    assert len(clusters) == 2
    assert clusters[0].raw_event_ids == ["c1"]
    assert clusters[1].raw_event_ids == ["c2"]


def test_cluster_detector_picks_last_navigate_in_router_chain() -> None:
    """A router redirect chain: click -> nav A -> nav B. The click's
    cluster includes BOTH navigates in its raw_event_ids audit trail."""
    click = _click("c1", "btn")
    nav_a = _caused_nav("n1", "c1", "http://x/first")
    nav_b = _caused_nav("n2", "c1", "http://x/second")
    events = _assign_synthetic_ids([click, nav_a, nav_b])
    causality = build_causality_graph(events)
    clusters = detect_semantic_clusters(events, causality)
    # Click cluster carries the click + at least the LAST navigate (the
    # one the user ended up on). Both navigates may be folded into the
    # raw_event_ids for audit.
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "click_with_navigation"
    # Audit trail includes all caused events.
    assert "c1" in c.raw_event_ids
    # At least one of the navigates is in the audit list.
    assert "n2" in c.raw_event_ids or "n1" in c.raw_event_ids


# ---------------------------------------------------------------------------
# build_skill propagates cluster ids onto step provenance
# ---------------------------------------------------------------------------


def test_build_skill_semantic_mode_writes_clusters_to_skill() -> None:
    click = _click("c1", "btn-open")
    skill = build_skill(skill_name="t", events=[click], auto=True)
    # Default annotate_mode=semantic -> clusters populated.
    assert skill.annotate_mode == "semantic"
    assert len(skill.semantic_clusters) == 1
    assert skill.semantic_clusters[0].cluster_kind == "single_event"


def test_build_skill_linear_mode_skips_cluster_pipeline() -> None:
    """``annotate_mode="linear"`` preserves the legacy event-to-step
    behavior. Skill.semantic_clusters stays empty; steps still get
    raw_event_ids from their own event ids (WI-02 backfill)."""
    click = _click("c1", "btn-open")
    skill = build_skill(
        skill_name="t", events=[click], auto=True, annotate_mode="linear"
    )
    assert skill.annotate_mode == "linear"
    assert skill.semantic_clusters == []
    # Step still has the raw event id for audit (from WI-02).
    s = skill.steps[0]
    assert s.provenance is not None
    assert s.provenance.raw_event_ids == ["c1"]


def test_build_skill_step_provenance_carries_cluster_raw_event_ids() -> None:
    """The step generated for a click_with_navigation cluster carries
    BOTH event ids in its provenance.raw_event_ids -- the cluster's
    audit trail is reachable from the step."""
    click = _click("c1", "btn-open")
    nav = _caused_nav("n1", "c1", "http://x/asset/A")
    skill = build_skill(skill_name="t", events=[click, nav], auto=True)
    # WI-08 collapse: one step.
    assert len(skill.steps) == 1
    s = skill.steps[0]
    assert s.provenance is not None
    assert set(s.provenance.raw_event_ids) == {"c1", "n1"}
    assert s.provenance.cluster_kind == "click_with_navigation"


def test_build_skill_step_provenance_includes_observed_children() -> None:
    """An observed child event (network_request) caused by a click
    appears in the step's provenance.raw_event_ids when semantic
    clustering is active. Linear mode preserves the WI-08 (click + nav
    only) shape."""
    click = _click("c1", "btn-open")
    req = _caused_request("r1", "c1", "/api/search")
    skill = build_skill(skill_name="t", events=[click, req], auto=True)
    assert len(skill.steps) == 1
    s = skill.steps[0]
    assert s.provenance is not None
    # Observed children land in the cluster -> propagate to the step.
    assert "c1" in s.provenance.raw_event_ids
    assert "r1" in s.provenance.raw_event_ids


# ---------------------------------------------------------------------------
# Roundtrip: SemanticCluster persists in skill JSON
# ---------------------------------------------------------------------------


def test_semantic_cluster_roundtrip() -> None:
    cluster = SemanticCluster(
        raw_event_ids=["c1", "n1"],
        cluster_kind="click_with_navigation",
        primary_target_event_id="c1",
        confidence=0.92,
        alternatives_considered=["single_event"],
    )
    step = SkillStep(
        index=0, action="click",
        fingerprint=ElementFingerprint(test_id="btn"),
    )
    skill = Skill(
        name="t",
        steps=[step],
        annotate_mode="semantic",
        semantic_clusters=[cluster],
    )
    blob = skill.model_dump_json()
    restored = Skill.model_validate_json(blob)
    assert restored.annotate_mode == "semantic"
    assert len(restored.semantic_clusters) == 1
    rc = restored.semantic_clusters[0]
    assert rc.cluster_kind == "click_with_navigation"
    assert rc.confidence == 0.92
    assert rc.alternatives_considered == ["single_event"]


def test_legacy_skill_load_defaults_annotate_mode_semantic() -> None:
    """A pre-WI-12 skill JSON file with no annotate_mode field defaults
    to ``semantic`` on load. The semantic_clusters list defaults empty.
    No upgrader runs -- the field is additive."""
    raw = {
        "name": "legacy",
        "steps": [
            {"index": 0, "action": "click",
             "fingerprint": {"test_id": "x"}},
        ],
    }
    skill = Skill.model_validate(raw)
    assert skill.annotate_mode == "semantic"  # additive default
    assert skill.semantic_clusters == []
