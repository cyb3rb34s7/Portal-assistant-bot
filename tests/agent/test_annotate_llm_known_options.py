"""id+label sprint: the annotate-LLM stage must surface a multi-select
param's option universe (id+label) into the LLM context AND carry the
label list onto the v2 param so a future planner clarify step can offer
the operator the seen labels.

These tests stub the LLM call (no network) and assert the producer
(prompt context) + consumer (v2 param ``label_options``) sides.
"""

from __future__ import annotations

import asyncio

import pilot.agent.annotate_llm as annotate_llm
from pilot.agent.annotate_llm import _LlmAnnotation, _LlmParam
from pilot.agent.schemas.skill import SkillFile


def _v1_skill_with_multiselect() -> dict:
    return {
        "name": "country_pick",
        "description": "",
        "params": [
            {
                "name": "country",
                "type": "string_list",
                "example": "Argentina",
                "required": True,
            }
        ],
        "steps": [
            {
                "index": 0,
                "action": "set_selection",
                "semantic_label": "set_selection_multiselect_country",
                "set_selection": {
                    "mode": "replace",
                    "param": "country",
                    "known_options": [
                        {"value": "ar", "label": "Argentina"},
                        {"value": "au", "label": "Australia"},
                        {"value": "zw", "label": "Zimbabwe"},
                    ],
                    "item_labels": {"ar": "Argentina"},
                },
            }
        ],
    }


def _patch_llm(monkeypatch, captured: dict):
    """Stub complete_structured: capture the user prompt, return a canned
    annotation that renames country -> country (semantic)."""

    async def _fake_complete_structured(client, *, messages, **_kw):
        # The user message carries the param summary we want to inspect.
        captured["user_prompt"] = next(
            m.content for m in messages if m.role == "user"
        )
        return _LlmAnnotation(
            description="Pick countries",
            parameters=[
                _LlmParam(
                    original_name="country",
                    semantic_name="country",
                    semantic_description="Countries to select (by label)",
                    type="string",
                    source_hint="user-provided",
                )
            ],
            preconditions=[],
            destructive_step_indexes=[],
            success_assertions=[],
        )

    monkeypatch.setattr(
        annotate_llm, "complete_structured", _fake_complete_structured
    )


def test_known_options_surface_in_llm_context(monkeypatch) -> None:
    captured: dict = {}
    _patch_llm(monkeypatch, captured)

    asyncio.run(
        annotate_llm.annotate_skill(
            client=object(),
            v1_skill=_v1_skill_with_multiselect(),
            portal_id="sample_portal",
        )
    )

    prompt = captured["user_prompt"]
    # The id+label universe is surfaced for the multiselect param.
    assert "Argentina (ar)" in prompt
    assert "Zimbabwe (zw)" in prompt
    assert "replay value is the LABEL" in prompt


def test_v2_param_carries_label_options(monkeypatch) -> None:
    captured: dict = {}
    _patch_llm(monkeypatch, captured)

    v2 = asyncio.run(
        annotate_llm.annotate_skill(
            client=object(),
            v1_skill=_v1_skill_with_multiselect(),
            portal_id="sample_portal",
        )
    )
    country = next(p for p in v2["parameters"] if p["name"] == "country")
    assert country["label_options"] == ["Argentina", "Australia", "Zimbabwe"]

    # And it survives validation against the v2 SkillFile param schema
    # (the validation gate must accept label_options).
    sf = SkillFile.model_validate(
        {
            "id": "country_pick",
            "name": "country_pick",
            "description": "Pick countries",
            "parameters": v2["parameters"],
            "param_alias_map": v2.get("param_alias_map", {}),
        }
    )
    sp = next(p for p in sf.parameters if p.name == "country")
    assert sp.label_options == ["Argentina", "Australia", "Zimbabwe"]
