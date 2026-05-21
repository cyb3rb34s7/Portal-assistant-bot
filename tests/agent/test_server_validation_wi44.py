"""WI-44: server validation errors as observable, classifiable signals.

Plan acceptance check:
  A Save that triggers 'Title is required' inline error fails the
  step with the field + message in error_details, not a generic
  locator timeout.

Tests cover the deterministic pipeline (no live browser):
  - Schema: StepAssertion supports kind='validation_field' with
    validation_field_id / validation_level / message_pattern.
  - Annotator: _index_validation_errors turns grabber-emitted
    validation_invalid readiness events into StepAssertion(
    kind=validation_field) entries.
  - Annotator: build_skill stamps validation_field assertions onto
    the causing step's assert_after.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import _index_validation_errors, build_skill
from pilot.skill_models import (
    ElementFingerprint,
    SkillStep,
    StepAssertion,
    TraceEvent,
)


# ----- schema --------------------------------------------------------------


def test_step_assertion_validation_field_roundtrip() -> None:
    a = StepAssertion(
        kind="validation_field",
        selector='[data-testid="input-asset-title"]',
        validation_field_id="input-asset-title",
        validation_level="error",
        message_pattern="Title is required",
    )
    payload = a.model_dump()
    restored = StepAssertion(**payload)
    assert restored.kind == "validation_field"
    assert restored.validation_field_id == "input-asset-title"
    assert restored.validation_level == "error"
    assert restored.message_pattern == "Title is required"


def test_step_assertion_validation_field_optional_message() -> None:
    a = StepAssertion(
        kind="validation_field",
        selector="#title",
        validation_field_id="title",
        validation_level="error",
    )
    assert a.message_pattern is None


# ----- annotator indexer ---------------------------------------------------


def _validation_event(
    event_id: str = "m1",
    caused_by: str = "save1",
    sequence: int = 2,
    selector: str = '[data-testid="input-asset-title"]',
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="dom_mutation",
        page_url="http://x",
        event_id=event_id,
        interaction_id=caused_by,
        caused_by=caused_by,
        sequence=sequence,
        source="readiness_observer",
        raw_event_kind="readiness_transition",
        mutation_summary={
            "readiness": {
                "kind": "validation_invalid",
                "selector": selector,
                "value": "true",
            }
        },
    )


def test_index_validation_errors_translates_to_step_assertion() -> None:
    ev = _validation_event(
        "m1", "save1", 2, selector='[data-testid="input-asset-title"]'
    )
    out = _index_validation_errors([ev])
    assert "save1" in out
    assert len(out["save1"]) == 1
    a = out["save1"][0]
    assert a.kind == "validation_field"
    assert a.selector == '[data-testid="input-asset-title"]'
    assert a.validation_field_id == "input-asset-title"
    assert a.validation_level == "error"


def test_index_validation_errors_handles_id_selector() -> None:
    ev = _validation_event(selector="#title")
    out = _index_validation_errors([ev])
    a = out["save1"][0]
    assert a.validation_field_id == "title"


def test_index_validation_errors_skips_non_validation_readiness() -> None:
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="dom_mutation",
        page_url="http://x",
        event_id="m1",
        interaction_id="save1",
        caused_by="save1",
        sequence=2,
        source="readiness_observer",
        raw_event_kind="readiness_transition",
        mutation_summary={
            "readiness": {
                "kind": "field_enabled",  # different kind
                "selector": '[data-testid="btn-save"]',
                "value": "enabled",
            }
        },
    )
    out = _index_validation_errors([ev])
    assert out == {}


# ----- annotator end-to-end -----------------------------------------------


def test_annotator_stamps_validation_field_on_step() -> None:
    save_click = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id="btn-save",
            tag="button",
            text="Save",
            control_kind="button",
        ),
        page_url="http://x",
        event_id="save1",
        interaction_id="save1",
        caused_by=None,
        sequence=1,
        source="user_click",
        raw_event_kind="click",
    )
    validation = _validation_event("m1", "save1", 2)
    skill = build_skill(
        skill_name="save_with_validation",
        events=[save_click, validation],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    cs = [s for s in skill.steps if s.action == "click"][0]
    val_asserts = [
        a for a in cs.assert_after if a.kind == "validation_field"
    ]
    assert len(val_asserts) == 1
    assert val_asserts[0].validation_field_id == "input-asset-title"
