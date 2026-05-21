"""Schema roundtrip coverage for foundation drift fixes."""

from __future__ import annotations

from datetime import datetime

from pilot.skill_models import ElementFingerprint, TraceEvent


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
