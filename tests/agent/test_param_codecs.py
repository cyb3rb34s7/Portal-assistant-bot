"""WI-05: tests for typed parameter codecs + constraint validation.

Covers the acceptance check: "Date input records as date, slider as
number_range, checkbox as boolean, native select as enum, multiselect
as string_list."
"""

from __future__ import annotations

import json

import pytest

from pilot.param_codecs import (
    ParamValidationError,
    infer_param_type_and_codec,
    resolve_param,
)
from pilot.skill_models import (
    ElementFingerprint,
    OptionSnapshot,
    ParamConstraints,
    Skill,
    SkillParam,
    SkillStep,
    TraceEvent,
)


# ---------------------------------------------------------------------------
# Codec behavior
# ---------------------------------------------------------------------------


class TestIsoDateCodec:
    def test_iso_passthrough(self):
        p = SkillParam(name="publish_at", type="date", codec="iso_date")
        assert resolve_param(p, "2025-11-12") == "2025-11-12"

    def test_us_format_to_iso(self):
        p = SkillParam(name="publish_at", type="date", codec="iso_date")
        assert resolve_param(p, "11/12/2025") == "2025-11-12"

    def test_unrecognized_raises(self):
        p = SkillParam(name="publish_at", type="date", codec="iso_date")
        with pytest.raises(ParamValidationError) as exc:
            resolve_param(p, "not a date")
        assert exc.value.param_name == "publish_at"


class TestBooleanCodec:
    def test_truthy(self):
        p = SkillParam(name="featured", type="boolean", codec="boolean")
        for raw in (True, 1, "true", "TRUE", "Yes", "on"):
            assert resolve_param(p, raw) == "true"

    def test_falsy(self):
        p = SkillParam(name="featured", type="boolean", codec="boolean")
        for raw in (False, 0, "false", "no", "off"):
            assert resolve_param(p, raw) == "false"

    def test_bogus_raises(self):
        p = SkillParam(name="featured", type="boolean", codec="boolean")
        with pytest.raises(ParamValidationError):
            resolve_param(p, "maybe")


class TestEnumCodec:
    def _opts(self):
        return [
            OptionSnapshot(value="DRAFT", label="Draft"),
            OptionSnapshot(value="PUBLISHED", label="Published"),
            OptionSnapshot(value="ARCHIVED", label="Archived"),
        ]

    def test_label_lookup(self):
        p = SkillParam(
            name="status", type="enum", codec="enum_label",
            enum_options=self._opts(),
        )
        assert resolve_param(p, "Published") == "PUBLISHED"

    def test_value_lookup_fallback(self):
        p = SkillParam(
            name="status", type="enum", codec="enum_label",
            enum_options=self._opts(),
        )
        # Operator passed the value, codec should still resolve.
        assert resolve_param(p, "DRAFT") == "DRAFT"

    def test_unknown_raises_with_available_list(self):
        p = SkillParam(
            name="status", type="enum", codec="enum_label",
            enum_options=self._opts(),
        )
        with pytest.raises(ParamValidationError) as exc:
            resolve_param(p, "Pending")
        assert "available_labels" in exc.value.details
        assert "Published" in exc.value.details["available_labels"]


class TestLocalizedNumberCodec:
    def test_plain(self):
        p = SkillParam(
            name="weight", type="number", codec="localized_number",
        )
        assert resolve_param(p, "12.5") == "12.5"

    def test_us_thousands(self):
        p = SkillParam(
            name="weight", type="number", codec="localized_number",
        )
        assert resolve_param(p, "1,234.5") == "1234.5"

    def test_eu_decimal(self):
        p = SkillParam(
            name="weight", type="number", codec="localized_number",
        )
        assert resolve_param(p, "1.234,5") == "1234.5"


class TestStringListCodec:
    def test_list_passthrough(self):
        p = SkillParam(name="cats", type="string_list")
        assert resolve_param(p, ["sports", "drama"]) == ["sports", "drama"]

    def test_comma_split(self):
        p = SkillParam(name="cats", type="string_list")
        assert resolve_param(p, "sports, drama") == ["sports", "drama"]


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


class TestConstraints:
    def test_allowed_values_scalar(self):
        p = SkillParam(
            name="status",
            type="enum",
            codec="enum_value",
            constraints=ParamConstraints(allowed_values=["A", "B"]),
        )
        assert resolve_param(p, "A") == "A"
        with pytest.raises(ParamValidationError) as exc:
            resolve_param(p, "C")
        assert "allowed" in exc.value.details

    def test_number_min_max(self):
        p = SkillParam(
            name="qty",
            type="number",
            codec="localized_number",
            constraints=ParamConstraints(min=1, max=100),
        )
        resolve_param(p, "50")
        with pytest.raises(ParamValidationError):
            resolve_param(p, "0")
        with pytest.raises(ParamValidationError):
            resolve_param(p, "200")

    def test_required_shape(self):
        p = SkillParam(
            name="asset_id",
            type="string",
            constraints=ParamConstraints(required_shape=r"[A-Z]-\d{4}"),
        )
        resolve_param(p, "A-9001")
        with pytest.raises(ParamValidationError):
            resolve_param(p, "garbage")

    def test_list_length_bounds(self):
        p = SkillParam(
            name="cats",
            type="string_list",
            constraints=ParamConstraints(list_min_len=1, list_max_len=3),
        )
        resolve_param(p, ["sports"])
        with pytest.raises(ParamValidationError):
            resolve_param(p, [])
        with pytest.raises(ParamValidationError):
            resolve_param(p, ["a", "b", "c", "d"])

    def test_list_allowed_values_per_item(self):
        p = SkillParam(
            name="cats",
            type="string_list",
            constraints=ParamConstraints(
                allowed_values=["sports", "drama", "kids"],
            ),
        )
        resolve_param(p, ["sports", "drama"])
        with pytest.raises(ParamValidationError):
            resolve_param(p, ["sports", "comedy"])


# ---------------------------------------------------------------------------
# Annotator inference (the acceptance check from WI-05)
# ---------------------------------------------------------------------------


class TestInference:
    def test_date_input(self):
        t, c = infer_param_type_and_codec(
            control_kind="date_input",
            value_kind="date",
            recorded_value="2025-01-01",
            has_options_snapshot=False,
        )
        assert (t, c) == ("date", "iso_date")

    def test_slider(self):
        t, c = infer_param_type_and_codec(
            control_kind="range_slider",
            value_kind="number",
            recorded_value="42",
            has_options_snapshot=False,
        )
        assert t == "number_range"
        assert c == "localized_number"

    def test_checkbox(self):
        t, c = infer_param_type_and_codec(
            control_kind="checkbox",
            value_kind="boolean",
            recorded_value="true",
            has_options_snapshot=False,
        )
        assert (t, c) == ("boolean", "boolean")

    def test_native_select(self):
        t, c = infer_param_type_and_codec(
            control_kind="select_single",
            value_kind="string",
            recorded_value="DRAFT",
            has_options_snapshot=True,
        )
        assert (t, c) == ("enum", "enum_value")

    def test_multiselect(self):
        t, c = infer_param_type_and_codec(
            control_kind="select_multiple",
            value_kind="list",
            recorded_value=None,
            has_options_snapshot=True,
        )
        assert t == "string_list"

    def test_legacy_no_metadata_falls_back_to_string(self):
        t, c = infer_param_type_and_codec(
            control_kind=None,
            value_kind=None,
            recorded_value="anything",
            has_options_snapshot=False,
        )
        assert (t, c) == ("string", "raw")


# ---------------------------------------------------------------------------
# Roundtrip (schema-then-forget guard from anti-drift checklist)
# ---------------------------------------------------------------------------


def test_skill_param_typed_roundtrip():
    p = SkillParam(
        name="status",
        type="enum",
        codec="enum_label",
        description="Asset status",
        example="DRAFT",
        required=True,
        enum_options=[
            OptionSnapshot(value="DRAFT", label="Draft"),
            OptionSnapshot(value="PUBLISHED", label="Published"),
        ],
        constraints=ParamConstraints(allowed_values=["DRAFT", "PUBLISHED"]),
    )
    dumped = p.model_dump_json()
    restored = SkillParam.model_validate_json(dumped)
    assert restored.type == "enum"
    assert restored.codec == "enum_label"
    assert restored.constraints is not None
    assert restored.constraints.allowed_values == ["DRAFT", "PUBLISHED"]
    assert restored.enum_options and restored.enum_options[1].label == "Published"


def test_legacy_skill_param_still_loads():
    """Backwards compat: a v1 SkillParam dict without codec / constraints
    / enum_options must load without errors and behave as raw string."""
    legacy = {
        "name": "title",
        "type": "string",
        "description": "Asset title",
        "example": "Hello",
        "required": True,
    }
    p = SkillParam.model_validate(legacy)
    assert p.codec == "raw"
    assert p.constraints is None
    assert resolve_param(p, "anything") == "anything"
