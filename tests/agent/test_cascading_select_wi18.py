"""WI-18: cascading select dependency model.

Plan acceptance check:
  Change region=APAC, replay picks Market from APAC list; recorded
  California (USA) under India fails cleanly with available Indian
  markets. NO fuzzy fallback on cascading deps.

Tests:
  - Schema roundtrip: DependencyChain persists in skill JSON.
  - Detector: parent_select change -> network_request -> child_select
    change yields a (parent, child, request) triple.
  - Annotator: build_skill stamps DependencyChain on the parent step
    AND depends_on on the child SkillParam.
  - Negative: two unrelated selects (no request between them) do NOT
    produce a cascading chain.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pilot.annotate import (
    _assign_synthetic_ids,
    _detect_cascading_select,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    DependencyChain,
    ElementFingerprint,
    NetworkExpectation,
    OptionSnapshot,
    Skill,
    SkillStep,
    TraceEvent,
)


def _sel(
    event_id: str,
    test_id: str,
    name_attr: str,
    value: str,
    options: list[dict[str, Any]],
    sequence: int = 1,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            name=name_attr,
            tag="select",
            control_kind="select_single",
            options_snapshot=[
                OptionSnapshot(value=o["value"], label=o["label"])
                for o in options
            ],
            selected_options=[value],
        ),
        value=value,
        page_url="http://x/asset",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_change",
    )


def _request(
    event_id: str, caused_by: str, url: str, sequence: int = 2
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="network_request",
        page_url="http://x/asset",
        event_id=event_id,
        caused_by=caused_by,
        interaction_id=caused_by,
        sequence=sequence,
        request_id=event_id,
        method="GET",
        url=url,
        source="fetch_request_start",
    )


# ----- schema --------------------------------------------------------------


def test_dependency_chain_roundtrip() -> None:
    dep = DependencyChain(
        parent_param="region",
        child_param="market",
        option_source_request=NetworkExpectation(
            url_pattern="/api/markets", method="GET"
        ),
        child_options_signature_after="[data-testid='select-market']",
    )
    step = SkillStep(
        index=0,
        action="select_option",
        fingerprint=ElementFingerprint(test_id="select-region"),
        dependency_chain=dep,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0]
    assert rs.dependency_chain is not None
    assert rs.dependency_chain.parent_param == "region"
    assert rs.dependency_chain.child_param == "market"
    assert rs.dependency_chain.option_source_request is not None
    assert (
        rs.dependency_chain.option_source_request.url_pattern
        == "/api/markets"
    )


# ----- detector -------------------------------------------------------------


def test_detector_emits_parent_child_chain_with_intervening_request() -> None:
    events = _assign_synthetic_ids([
        _sel(
            "p1", "select-region", "region", "APAC",
            [{"value": "APAC", "label": "APAC"}, {"value": "EMEA", "label": "EMEA"}],
            sequence=1,
        ),
        _request("r1", "p1", "/api/markets?region=APAC", sequence=2),
        _sel(
            "c1", "select-market", "market", "JP",
            [{"value": "JP", "label": "Japan"}, {"value": "IN", "label": "India"}],
            sequence=3,
        ),
    ])
    causality = build_causality_graph(events)
    chains = _detect_cascading_select(events, causality, set())
    assert len(chains) == 1
    parent_eid, child_eid, req_ev = chains[0]
    assert parent_eid == "p1"
    assert child_eid == "c1"
    assert req_ev is not None
    assert req_ev.event_id == "r1"


def test_detector_skips_pair_without_request_between() -> None:
    """Two selects with no intervening network request from the
    parent do NOT form a cascading chain."""
    events = _assign_synthetic_ids([
        _sel(
            "p1", "select-a", "a", "X",
            [{"value": "X", "label": "X"}],
            sequence=1,
        ),
        _sel(
            "p2", "select-b", "b", "Y",
            [{"value": "Y", "label": "Y"}],
            sequence=2,
        ),
    ])
    causality = build_causality_graph(events)
    chains = _detect_cascading_select(events, causality, set())
    assert chains == []


# ----- annotator integration ----------------------------------------------


def test_build_skill_stamps_dependency_chain_on_parent_step() -> None:
    events = [
        _sel(
            "p1", "select-region", "region", "APAC",
            [{"value": "APAC", "label": "APAC"}, {"value": "EMEA", "label": "EMEA"}],
            sequence=1,
        ),
        _request("r1", "p1", "/api/markets?region=APAC", sequence=2),
        _sel(
            "c1", "select-market", "market", "JP",
            [{"value": "JP", "label": "Japan"}],
            sequence=3,
        ),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    so = [s for s in skill.steps if s.action == "select_option"]
    assert len(so) == 2
    parent_step, child_step = so[0], so[1]
    # Parent has dependency_chain set.
    assert parent_step.dependency_chain is not None
    assert (
        parent_step.dependency_chain.option_source_request is not None
    )
    assert (
        parent_step.dependency_chain.option_source_request.url_pattern
        == "/api/markets"
    )
    # Child step has its own select_option spec; no chain on child.
    assert child_step.dependency_chain is None


def test_build_skill_stamps_depends_on_on_child_param() -> None:
    events = [
        _sel(
            "p1", "select-region", "region", "APAC",
            [{"value": "APAC", "label": "APAC"}],
            sequence=1,
        ),
        _request("r1", "p1", "/api/markets?region=APAC", sequence=2),
        _sel(
            "c1", "select-market", "market", "JP",
            [{"value": "JP", "label": "Japan"}],
            sequence=3,
        ),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    params_by_name = {p.name: p for p in skill.params}
    # The child param "market" should declare depends_on = "region".
    assert "market" in params_by_name
    assert "region" in params_by_name
    assert params_by_name["market"].depends_on == "region"
    # Parent param has no depends_on.
    assert params_by_name["region"].depends_on is None
