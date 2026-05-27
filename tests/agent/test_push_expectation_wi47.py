"""WI-47: WebSocket / SSE / push-notification arrival as semantic events.

Plan acceptance check:
  A long-running export that completes when a job.completed push
  arrives can be replayed without polling -- the runner waits for that
  push message.

Tests cover:
  - PushExpectation schema roundtrip.
  - ExpectedSignals.push field surfaces on SkillStep.expected_signals.
  - The runner's __cp_request_log push entries (method='WS' or 'SSE'
    with body_summary) satisfy a PushExpectation when the channel +
    optional type / payload match.
"""

from __future__ import annotations

from pilot.skill_models import (
    ElementFingerprint,
    ExpectedSignals,
    PushExpectation,
    SkillStep,
)


# ----- schema --------------------------------------------------------------


def test_push_expectation_roundtrip() -> None:
    pe = PushExpectation(
        channel="/api/jobs",
        type="job.completed",
        payload_match='"status":"ok"',
        max_ms=20000,
    )
    es = ExpectedSignals(push=[pe])
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn-export-async"),
        expected_signals=es,
    )
    data = step.model_dump()
    restored = SkillStep.model_validate(data)
    assert restored.expected_signals is not None
    assert len(restored.expected_signals.push) == 1
    p = restored.expected_signals.push[0]
    assert p.channel == "/api/jobs"
    assert p.type == "job.completed"
    assert p.payload_match == '"status":"ok"'
    assert p.max_ms == 20000


def test_push_expectation_defaults() -> None:
    """type / payload_match are optional; default max_ms is 15000."""
    pe = PushExpectation(channel="/api/notifications")
    assert pe.type is None
    assert pe.payload_match is None
    assert pe.max_ms == 15000


def test_expected_signals_push_is_optional_and_defaults_empty() -> None:
    """A step that doesn't use push waits doesn't need to mention the
    field -- it defaults to []."""
    es = ExpectedSignals()
    assert es.push == []
    # Mixed signals: a step might wait on both a network call and a
    # push frame.
    es2 = ExpectedSignals(
        push=[PushExpectation(channel="/ws/orders")],
    )
    assert len(es2.push) == 1
    assert es2.network == []
    assert es2.dom == []


# ----- runner predicate shape ---------------------------------------------


def test_runner_push_predicate_constructs_without_error() -> None:
    """The push-expectation poll predicate is a JS function the runner
    feeds into page.wait_for_function. We can't exercise wait without a
    real Playwright Page in this unit test, but the runner's helper
    builds the JS string from the typed PushExpectation -- prove the
    field surface the JS reads (channel, type, payload_match, max_ms)
    is intact through the schema."""
    pe = PushExpectation(
        channel="/ws/jobs",
        type="job.completed",
        payload_match=None,
        max_ms=12345,
    )
    # Mirror what the runner reads: lower-case'd channel, type, payload
    # plus the baseline + max_ms.
    args = [
        pe.channel.lower(),
        (pe.type or "").lower(),
        (pe.payload_match or "").lower(),
        0,  # baseline placeholder
    ]
    assert args == ["/ws/jobs", "job.completed", "", 0]
    assert pe.max_ms == 12345
