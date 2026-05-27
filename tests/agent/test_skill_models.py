"""Schema roundtrip coverage for foundation drift fixes."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from pilot.skill_models import (
    ElementFingerprint,
    NetworkExpectation,
    ParamProvenance,
    ReplayPolicy,
    TraceEvent,
)


def test_option_snapshot_roundtrips_bool_fields() -> None:
    """Select option snapshots preserve bool producer fields."""
    fp = ElementFingerprint.model_validate(
        {
            "tag": "select",
            "control_kind": "select_single",
            "value_kind": "string",
            "options_snapshot": [
                {
                    "value": "emea",
                    "label": "EMEA",
                    "selected": True,
                    "disabled": False,
                },
                {
                    "value": "apac",
                    "label": "APAC",
                    "selected": False,
                    "disabled": True,
                },
            ],
            "selected_options": ["emea"],
        }
    )

    restored = ElementFingerprint.model_validate_json(fp.model_dump_json())

    assert restored == fp
    assert restored.options_snapshot is not None
    assert restored.options_snapshot[0].selected is True
    assert restored.options_snapshot[1].disabled is True


def test_trace_event_page_state_roundtrip() -> None:
    """F-06: page_state_before / page_state_after carry url, title, and
    key_dom_signature from the grabber. The fields must survive
    JSONL serialization so the annotator can read them."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        page_url="http://x",
        event_id="c1",
        page_state_before={
            "url": "http://x/before",
            "title": "Before page",
            "key_dom_signature": "deadbeef",
        },
        page_state_after={
            "url": "http://x/after",
            "title": "After page",
            "key_dom_signature": "cafef00d",
        },
    )

    restored = TraceEvent.model_validate_json(ev.model_dump_json())

    assert restored.page_state_before == {
        "url": "http://x/before",
        "title": "Before page",
        "key_dom_signature": "deadbeef",
    }
    assert restored.page_state_after == {
        "url": "http://x/after",
        "title": "After page",
        "key_dom_signature": "cafef00d",
    }


def test_trace_event_page_state_optional() -> None:
    """Legacy traces (no page_state) still load cleanly."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        page_url="http://x",
    )
    restored = TraceEvent.model_validate_json(ev.model_dump_json())
    assert restored.page_state_before is None
    assert restored.page_state_after is None


def test_trace_event_network_request_roundtrip() -> None:
    """F-07: network_request and network_response events carry the
    request_id + method + url + started_at + finished_at + status +
    initiator_event_id needed by WI-09 expected_signals.started_after_event."""
    req = TraceEvent(
        ts=datetime.utcnow(),
        kind="network_request",
        page_url="http://x",
        event_id="req-evt-1",
        request_id="req-1",
        method="POST",
        url="http://x/api/assets",
        started_at=12345.6,
        initiator_event_id="click-1",
    )
    resp = TraceEvent(
        ts=datetime.utcnow(),
        kind="network_response",
        page_url="http://x",
        event_id="resp-evt-1",
        request_id="req-1",
        method="POST",
        url="http://x/api/assets",
        started_at=12345.6,
        finished_at=12350.2,
        status=200,
        initiator_event_id="click-1",
    )
    req_r = TraceEvent.model_validate_json(req.model_dump_json())
    resp_r = TraceEvent.model_validate_json(resp.model_dump_json())
    assert req_r.kind == "network_request"
    assert req_r.request_id == "req-1"
    assert req_r.initiator_event_id == "click-1"
    assert req_r.method == "POST"
    assert resp_r.kind == "network_response"
    assert resp_r.status == 200
    assert resp_r.finished_at == 12350.2


def test_replay_policy_normalizes_optional_true_to_on_failure_optional() -> None:
    """F-09a: optional=True with the default on_failure='abort' is
    contradictory. Normalize so the two fields can't disagree at
    runtime."""
    policy = ReplayPolicy(optional=True)  # default on_failure="abort"
    assert policy.optional is True
    assert policy.on_failure == "optional"


def test_replay_policy_preserves_explicit_on_failure_continue() -> None:
    """F-09a: when optional=True is paired with a non-abort on_failure
    that is itself non-conflicting, leave it alone."""
    policy = ReplayPolicy(optional=True, on_failure="continue")
    assert policy.on_failure == "continue"


def test_replay_policy_default_is_abort_not_optional() -> None:
    """Default fail-fast policy from WI-07 still holds."""
    policy = ReplayPolicy()
    assert policy.optional is False
    assert policy.on_failure == "abort"


def test_param_provenance_confidence_range_enforced() -> None:
    """F-09b: confidence must be in [0.0, 1.0]."""
    ParamProvenance(source="operator_input", confidence=0.0)
    ParamProvenance(source="operator_input", confidence=1.0)
    ParamProvenance(source="operator_input", confidence=0.5)
    ParamProvenance(source="operator_input", confidence=None)
    with pytest.raises(ValidationError):
        ParamProvenance(source="operator_input", confidence=1.5)
    with pytest.raises(ValidationError):
        ParamProvenance(source="operator_input", confidence=-0.1)


def test_network_expectation_started_after_event_field_present() -> None:
    """F-09d / F-07: schema slot for action-scoped baseline."""
    ne = NetworkExpectation(
        url_pattern="/api/x",
        started_after_event="evt-click-1",
    )
    assert ne.started_after_event == "evt-click-1"
    # Default is None -- legacy skills don't carry the field.
    ne2 = NetworkExpectation(url_pattern="/api/x")
    assert ne2.started_after_event is None


def test_trace_event_dom_mutation_roundtrip() -> None:
    """F-07: dom_mutation events carry a debounced burst summary so
    WI-09 / WI-10 can wait on real DOM activity without rescanning
    every MutationRecord."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="dom_mutation",
        page_url="http://x",
        event_id="mut-1",
        mutation_summary={
            "added": 3,
            "removed": 1,
            "attribute": 5,
            "character_data": 0,
            "first_target_selector": "div#root > main > .panel",
        },
    )
    restored = TraceEvent.model_validate_json(ev.model_dump_json())
    assert restored.kind == "dom_mutation"
    assert restored.mutation_summary["added"] == 3
    assert restored.mutation_summary["first_target_selector"] == "div#root > main > .panel"
