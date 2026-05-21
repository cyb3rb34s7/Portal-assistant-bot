"""Schema roundtrip coverage for foundation drift fixes."""

from __future__ import annotations

from pilot.skill_models import ElementFingerprint


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
