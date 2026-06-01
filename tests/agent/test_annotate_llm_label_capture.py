"""2026-06-02 batch 2/B2.7: LLM annotation must surface the new
batch-1 grabber fields (accessible_name, current_value, known_options
from select_option) into the prompt context so the LLM can reason
about labeled widgets rather than guessing from testid alone.

The LLM call itself is stubbed (no network) -- these tests assert the
producer (prompt content) side and the validation gate.
"""

from __future__ import annotations

import asyncio

import pilot.agent.annotate_llm as annotate_llm
from pilot.agent.annotate_llm import _LlmAnnotation, _LlmParam


def _v1_skill_with_labeled_widgets() -> dict:
    """A skill whose params are bound from CLICK events on labeled
    mat-select widgets (batch-1 + batch-2 annotator output shape)."""
    return {
        "name": "transfer_artwork_angular",
        "description": "",
        "params": [
            {
                "name": "year",
                "type": "enum",
                "example": "2024",
                "required": True,
            },
            {
                "name": "target_model",
                "type": "enum",
                "example": "24_KANTM2_8K",
                "required": True,
            },
        ],
        "steps": [
            {
                "index": 0,
                "action": "click",
                "semantic_label": "click_year_picker",
                "param_binding": {"name": "year", "type": "string"},
                "fingerprint": {
                    "tag": "mat-select",
                    "role": "listbox",
                    "accessible_name": "Year*: ",
                    "current_value": "2024",
                    "element_id": "mat-select-year",
                },
                "select_option": {
                    "recorded_value": "2024",
                    "recorded_label": "2024",
                    "known_options": [
                        {"value": "2024", "label": "2024"},
                        {"value": "2023", "label": "2023"},
                        {"value": "2022", "label": "2022"},
                    ],
                },
            },
            {
                "index": 1,
                "action": "click",
                "semantic_label": "click_model_picker",
                "param_binding": {"name": "target_model", "type": "string"},
                "fingerprint": {
                    "tag": "mat-select",
                    "role": "listbox",
                    "accessible_name": "Target Model*: ",
                    "current_value": "24_KANTM2_8K",
                    "element_id": "mat-select-model",
                },
                "select_option": {
                    "recorded_value": "24_KANTM2_8K",
                    "recorded_label": "24_KANTM2_8K",
                    "known_options": [
                        {"value": "24_BOMRB_8K", "label": "24_BOMRB_8K"},
                        {"value": "24_KANTM2_8K", "label": "24_KANTM2_8K"},
                    ],
                },
            },
        ],
    }


def _patch_llm(monkeypatch, captured: dict):
    async def _fake_complete_structured(client, *, messages, **_kw):
        captured["user_prompt"] = next(
            m.content for m in messages if m.role == "user"
        )
        captured["sys_prompt"] = next(
            m.content for m in messages if m.role == "system"
        )
        return _LlmAnnotation(
            description="Transfer artwork between models",
            parameters=[
                _LlmParam(
                    original_name="year",
                    semantic_name="release_year",
                    semantic_description="Year of the target model",
                    type="number",
                    source_hint="operator-provided",
                ),
                _LlmParam(
                    original_name="target_model",
                    semantic_name="target_model_code",
                    semantic_description="Internal code for the target Frame TV model",
                    type="string",
                    source_hint="operator-provided",
                ),
            ],
            preconditions=[],
            destructive_step_indexes=[],
            success_assertions=[],
        )

    monkeypatch.setattr(
        annotate_llm, "complete_structured", _fake_complete_structured
    )


def test_accessible_name_in_llm_prompt(monkeypatch) -> None:
    """The labeled widget's accessible_name appears in the prompt so the
    LLM can ground its semantic_name proposal in the actual page label."""
    captured: dict = {}
    _patch_llm(monkeypatch, captured)
    asyncio.run(
        annotate_llm.annotate_skill(
            client=object(),
            v1_skill=_v1_skill_with_labeled_widgets(),
            portal_id="angular_sample",
        )
    )
    prompt = captured["user_prompt"]
    assert "Year*: " in prompt
    assert "Target Model*: " in prompt


def test_current_value_in_llm_prompt(monkeypatch) -> None:
    """The current_value (displayed widget value at record time) appears
    in the prompt -- the strongest disambiguator two unlabeled clicks
    on the same widget tag would otherwise be missing."""
    captured: dict = {}
    _patch_llm(monkeypatch, captured)
    asyncio.run(
        annotate_llm.annotate_skill(
            client=object(),
            v1_skill=_v1_skill_with_labeled_widgets(),
            portal_id="angular_sample",
        )
    )
    prompt = captured["user_prompt"]
    assert "2024" in prompt
    assert "24_KANTM2_8K" in prompt


def test_known_options_from_select_option_in_prompt(monkeypatch) -> None:
    """select_option's known_options (the mat-select option universe) is
    surfaced in the prompt so the LLM can read the param's domain."""
    captured: dict = {}
    _patch_llm(monkeypatch, captured)
    asyncio.run(
        annotate_llm.annotate_skill(
            client=object(),
            v1_skill=_v1_skill_with_labeled_widgets(),
            portal_id="angular_sample",
        )
    )
    prompt = captured["user_prompt"]
    assert "24_BOMRB_8K" in prompt
    assert "24_KANTM2_8K" in prompt


def test_sys_prompt_forbids_inventing_new_params(monkeypatch) -> None:
    """The system prompt explicitly tells the LLM not to invent params."""
    captured: dict = {}
    _patch_llm(monkeypatch, captured)
    asyncio.run(
        annotate_llm.annotate_skill(
            client=object(),
            v1_skill=_v1_skill_with_labeled_widgets(),
            portal_id="angular_sample",
        )
    )
    sys_prompt = captured["sys_prompt"]
    assert "Don't invent" in sys_prompt or "never reference a new param" in sys_prompt


def test_validation_gate_keeps_grounded_renames(monkeypatch) -> None:
    """The validation gate accepts an LLM-proposed semantic_name when
    the original v1 param is in the input list. Each LLM parameter MUST
    have original_name preserved verbatim; checking that's verified by
    the existing structural validation."""
    captured: dict = {}
    _patch_llm(monkeypatch, captured)
    v2 = asyncio.run(
        annotate_llm.annotate_skill(
            client=object(),
            v1_skill=_v1_skill_with_labeled_widgets(),
            portal_id="angular_sample",
        )
    )
    semantic_names = {p["name"] for p in v2["parameters"]}
    assert "release_year" in semantic_names
    assert "target_model_code" in semantic_names
    # Param alias map preserves the v1 original names.
    assert v2["param_alias_map"]["release_year"] == "year"
    assert v2["param_alias_map"]["target_model_code"] == "target_model"
