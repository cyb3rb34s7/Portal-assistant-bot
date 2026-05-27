"""WI-31: LLM annotate enriches STRUCTURE, not only names.

Plan acceptance check:
  LLM pass can rename params + confirm a detected cascade/multiselect;
  CANNOT add a dependency / expected_signal with no supporting trace
  evidence.

Tests cover validate_overlays (no live LLM needed):
  - StructuralOverlay roundtrip through SkillFile.
  - Overlay with valid step_index + valid depends_on accepted.
  - Overlay with step_index out of range -> rejected.
  - Overlay with depends_on pointing AHEAD of itself (later step) ->
    that dependency edge is dropped.
  - Overlay with expected_signal.network referencing a non-recorded
    URL is dropped when trace_events is provided (strict mode).
  - Overlay with expected_signal.network matching a recorded
    network_request URL is kept.
  - Overlay with hallucinated depends_on (non-numeric) is dropped.
"""

from __future__ import annotations

from pilot.agent.annotate_llm import validate_overlays
from pilot.agent.schemas.skill import SkillFile, StructuralOverlay


# ----- schema --------------------------------------------------------------


def test_structural_overlay_roundtrip() -> None:
    overlay = StructuralOverlay(
        step_index=2,
        depends_on=["0", "1"],
        expected_signals=[
            {"kind": "network", "url_pattern": "/api/markets"},
        ],
        assert_after=[{"type": "text_visible", "text": "Saved"}],
        widget_type="cascading_select",
        risk_notes=["Market depends on Region; recorded value may not exist"],
        param_alias_map={"region": "slot_0_region"},
    )
    sf = SkillFile(
        id="t", name="t", structural_overlays=[overlay], steps=[{"index": 0}]
    )
    restored = SkillFile.model_validate_json(sf.model_dump_json())
    assert len(restored.structural_overlays) == 1
    o = restored.structural_overlays[0]
    assert o.step_index == 2
    assert o.depends_on == ["0", "1"]
    assert o.widget_type == "cascading_select"
    assert "Saved" in o.assert_after[0]["text"]


# ----- validation ----------------------------------------------------------


def _stub_skill(n_steps: int = 4) -> dict[str, object]:
    return {
        "steps": [
            {
                "index": i,
                "action": "change",
                "provenance": {"raw_event_ids": [f"e{i}"]},
            }
            for i in range(n_steps)
        ],
    }


def test_overlay_with_valid_step_and_depends_on_accepted() -> None:
    """Step 2 depends on step 0 -- valid (0 precedes 2)."""
    overlays = [
        {
            "step_index": 2,
            "depends_on": ["0"],
            "widget_type": "cascading_select",
        },
    ]
    accepted, rejected = validate_overlays(
        overlays, v1_skill=_stub_skill(4)
    )
    assert len(accepted) == 1
    assert accepted[0]["depends_on"] == ["0"]
    assert accepted[0]["widget_type"] == "cascading_select"
    assert rejected == []


def test_overlay_with_out_of_range_step_rejected() -> None:
    """step_index=99 doesn't exist -> overlay rejected entirely."""
    overlays = [{"step_index": 99, "depends_on": []}]
    accepted, rejected = validate_overlays(
        overlays, v1_skill=_stub_skill(4)
    )
    assert accepted == []
    assert len(rejected) == 1
    assert "step_index_out_of_range" in rejected[0]["rejection_reason"]


def test_overlay_with_forward_depends_on_drops_edge() -> None:
    """Step 1 depends on step 3 (a later step) -- impossible causally.
    The overlay is kept but the bad edge is dropped."""
    overlays = [{"step_index": 1, "depends_on": ["3"]}]
    accepted, rejected = validate_overlays(
        overlays, v1_skill=_stub_skill(4)
    )
    assert len(accepted) == 1
    assert accepted[0]["depends_on"] == []
    assert "3" in accepted[0]["_dropped"]["depends_on"]


def test_overlay_with_hallucinated_network_signal_dropped_strict() -> None:
    """LLM claims expected_signal.network with /api/widgets URL that
    NO recorded network_request matches -- dropped in strict mode."""
    overlays = [{
        "step_index": 1,
        "expected_signals": [
            {"kind": "network", "url_pattern": "/api/widgets"},
        ],
    }]
    trace_events = [
        {"kind": "network_request", "url": "/api/markets?region=APAC"},
    ]
    accepted, rejected = validate_overlays(
        overlays, v1_skill=_stub_skill(4), trace_events=trace_events
    )
    assert len(accepted) == 1
    assert accepted[0]["expected_signals"] == []
    dropped = accepted[0]["_dropped"]["expected_signals"]
    assert len(dropped) == 1
    assert "no_supporting_network_request" in dropped[0]["reason"]


def test_overlay_with_supported_network_signal_accepted_strict() -> None:
    """LLM claims a network signal whose pattern IS in a recorded
    network_request URL -- kept."""
    overlays = [{
        "step_index": 1,
        "expected_signals": [
            {"kind": "network", "url_pattern": "/api/markets"},
        ],
    }]
    trace_events = [
        {"kind": "network_request", "url": "/api/markets?region=APAC"},
    ]
    accepted, rejected = validate_overlays(
        overlays, v1_skill=_stub_skill(4), trace_events=trace_events
    )
    assert len(accepted) == 1
    assert accepted[0]["expected_signals"][0]["url_pattern"] == "/api/markets"


def test_overlay_with_non_numeric_depends_on_dropped() -> None:
    """LLM emits depends_on=['the previous step'] -- non-numeric, can't
    be a step index, dropped."""
    overlays = [{"step_index": 2, "depends_on": ["the previous step"]}]
    accepted, rejected = validate_overlays(
        overlays, v1_skill=_stub_skill(4)
    )
    assert len(accepted) == 1
    assert accepted[0]["depends_on"] == []


def test_overlay_lenient_mode_keeps_unbacked_signals_when_trace_absent() -> None:
    """Without trace_events, the validator can't strict-check network
    signals; it should still keep them but mark the overlay so the
    operator sees the lack of evidence isn't a rejection."""
    overlays = [{
        "step_index": 1,
        "expected_signals": [
            {"kind": "network", "url_pattern": "/api/anything"},
        ],
    }]
    accepted, rejected = validate_overlays(
        overlays, v1_skill=_stub_skill(4), trace_events=None
    )
    assert len(accepted) == 1
    assert len(accepted[0]["expected_signals"]) == 1


def test_overlay_assert_after_and_risk_notes_pass_through() -> None:
    """assert_after / widget_type / risk_notes are advisory; not
    strictly validated; pass through unchanged."""
    overlays = [{
        "step_index": 0,
        "assert_after": [{"type": "text_visible", "text": "Saved"}],
        "risk_notes": ["This is a destructive save"],
        "widget_type": "save_button",
        "param_alias_map": {"semantic_x": "v1_x"},
    }]
    accepted, _ = validate_overlays(
        overlays, v1_skill=_stub_skill(4)
    )
    assert accepted[0]["assert_after"] == [
        {"type": "text_visible", "text": "Saved"}
    ]
    assert accepted[0]["widget_type"] == "save_button"
    assert accepted[0]["risk_notes"] == ["This is a destructive save"]
    assert accepted[0]["param_alias_map"] == {"semantic_x": "v1_x"}
