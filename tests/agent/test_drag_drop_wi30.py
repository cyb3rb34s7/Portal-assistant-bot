"""WI-30: drag and drop recording + replay.

Plan acceptance check:
  Dragging an item into a drop zone records, replays, and asserts the
  target state contains the dragged item.

Tests cover the deterministic pipeline (no live browser):
  - Schema roundtrip: DragDropSpec persists.
  - Detector: dragstart -> dragover* -> drop collapses into ONE
    drag_drop cluster anchored on the drop event.
  - Detector: drop without paired dragstart is discarded (defensive).
  - Annotator: build_skill emits ONE drag_drop step with the
    source_fp / target_fp / data_payload_summary populated.
  - Runner stub: drag_drop is no longer in _UNIMPLEMENTED_ACTIONS.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_drag_drop_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    DragDropSpec,
    ElementFingerprint,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS


def _dragstart(event_id: str, sequence: int, source_id: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="dragstart",
        fingerprint=ElementFingerprint(test_id=source_id),
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_dragstart",
    )


def _dragover(event_id: str, sequence: int, target_id: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="dragover",
        fingerprint=ElementFingerprint(test_id=target_id),
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_dragover",
    )


def _drop(
    event_id: str, sequence: int, target_id: str, source_id: str = ""
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="drop",
        fingerprint=ElementFingerprint(test_id=target_id),
        drop_target_fp=ElementFingerprint(test_id=target_id),
        data_transfer_summary={
            "types": ["text/plain"],
            "text_plain": source_id,
            "drop_effect": "move",
        },
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_drop",
    )


# ----- schema --------------------------------------------------------------


def test_drag_drop_spec_roundtrip() -> None:
    spec = DragDropSpec(
        source_fp=ElementFingerprint(test_id="drag-item-promo-1"),
        target_fp=ElementFingerprint(test_id="drag-zone-excluded"),
        data_payload_summary={"types": ["text/plain"], "text_plain": "promo-1"},
        drop_effect="move",
        coordinates_policy="center",
        use_high_level_api=True,
    )
    step = SkillStep(
        index=0,
        action="drag_drop",
        fingerprint=ElementFingerprint(test_id="drag-zone-excluded"),
        drag_drop=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0].drag_drop
    assert rs is not None
    assert rs.source_fp.test_id == "drag-item-promo-1"
    assert rs.target_fp.test_id == "drag-zone-excluded"
    assert rs.drop_effect == "move"
    assert rs.coordinates_policy == "center"
    assert rs.use_high_level_api is True
    assert rs.data_payload_summary["text_plain"] == "promo-1"


# ----- detector ------------------------------------------------------------


def test_detector_collapses_dragstart_dragover_drop_into_one_cluster() -> None:
    """A dragstart -> 2 dragover -> drop sequence is one drag_drop
    cluster anchored on the drop event."""
    events = _assign_synthetic_ids([
        _dragstart("ds1", 1, "drag-item-promo-1"),
        _dragover("dv1", 2, "drag-zone-excluded"),
        _dragover("dv2", 3, "drag-zone-excluded"),
        _drop("dp1", 4, "drag-zone-excluded", source_id="promo-1"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_drag_drop_clusters(events, causality, consumed)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_kind == "drag_drop"
    assert c.primary_target_event_id == "dp1"
    assert c.raw_event_ids == ["ds1", "dv1", "dv2", "dp1"]
    # All four event ids are consumed so they don't fall through to
    # the single_event fallback.
    assert all(eid in consumed for eid in ("ds1", "dv1", "dv2", "dp1"))


def test_detector_drops_without_dragstart_are_discarded() -> None:
    """A drop event with no preceding dragstart is skipped -- browsers
    don't typically emit this but we should be defensive."""
    events = _assign_synthetic_ids([
        _drop("dp1", 1, "drag-zone-excluded"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_drag_drop_clusters(events, causality, consumed)
    assert clusters == []


def test_detector_handles_two_separate_drag_sequences() -> None:
    """Two distinct drag sequences produce two clusters."""
    events = _assign_synthetic_ids([
        _dragstart("ds1", 1, "drag-item-promo-1"),
        _drop("dp1", 2, "drag-zone-excluded"),
        _dragstart("ds2", 3, "drag-item-promo-2"),
        _drop("dp2", 4, "drag-zone-included"),
    ])
    causality = build_causality_graph(events)
    consumed: set[str] = set()
    clusters = _detect_drag_drop_clusters(events, causality, consumed)
    assert len(clusters) == 2
    assert clusters[0].primary_target_event_id == "dp1"
    assert clusters[1].primary_target_event_id == "dp2"


# ----- annotator integration ----------------------------------------------


def test_build_skill_emits_one_drag_drop_step_with_source_target() -> None:
    """A complete drag/drop sequence -> ONE drag_drop step with
    drag_drop spec carrying source + target fingerprints."""
    events = [
        _dragstart("ds1", 1, "drag-item-promo-1"),
        _dragover("dv1", 2, "drag-zone-excluded"),
        _drop("dp1", 3, "drag-zone-excluded", source_id="promo-1"),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    dd_steps = [s for s in skill.steps if s.action == "drag_drop"]
    assert len(dd_steps) == 1
    s = dd_steps[0]
    assert s.drag_drop is not None
    assert s.drag_drop.source_fp.test_id == "drag-item-promo-1"
    assert s.drag_drop.target_fp.test_id == "drag-zone-excluded"
    assert s.drag_drop.drop_effect == "move"


def test_build_skill_does_not_emit_stray_steps_for_drag_events() -> None:
    """The dragstart and dragover events get folded into the drag_drop
    step's raw_event_ids -- they should NOT appear as standalone
    single_event steps."""
    events = [
        _dragstart("ds1", 1, "drag-item-x"),
        _dragover("dv1", 2, "drag-zone-y"),
        _drop("dp1", 3, "drag-zone-y"),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    # Only one step: the drag_drop. No stray click/change/single_event.
    assert len(skill.steps) == 1
    assert skill.steps[0].action == "drag_drop"


# ----- runner --------------------------------------------------------------


def test_runner_no_longer_treats_drag_drop_as_unimplemented() -> None:
    assert "drag_drop" not in _UNIMPLEMENTED_ACTIONS
