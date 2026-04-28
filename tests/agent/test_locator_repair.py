"""Unit tests for pilot.agent.locator_repair.

Tests the deterministic backend with a mocked Page. Confirms:
  - High-similarity match returns confidence=high + a fingerprint
  - Mid-range match returns medium
  - Below threshold returns low (and locator=None — caller refuses)
  - Empty page returns low

The LLM backend is exercised via a fake complete_structured + Message
pair so we don't depend on a live AIClient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pilot.agent.locator_repair import (
    HealResult,
    LocatorRepair,
)
from pilot.skill_models import ElementFingerprint


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeLocator:
    xpath: str

    @property
    def first(self):
        return self


class _FakePage:
    """Minimal Page stand-in. .evaluate() returns a fixed list of
    candidate elements. .locator() returns a placeholder locator
    so HealResult.locator is non-None for the test assertions."""

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self.candidates = candidates

    def evaluate(self, _js: str) -> list[dict[str, Any]]:
        return self.candidates

    def locator(self, sel: str) -> _FakeLocator:
        return _FakeLocator(xpath=sel)


# ---------------------------------------------------------------------------
# Deterministic backend
# ---------------------------------------------------------------------------


def test_deterministic_high_match_via_testid() -> None:
    fp = ElementFingerprint(
        test_id="btn-save-layout",
        accessible_name="Save Layout",
        role="button",
        tag="button",
    )
    page = _FakePage([
        {
            "tag": "button",
            "id": None,
            "testId": "btn-save-layout",  # exact match — top score
            "role": "button",
            "ariaLabel": None,
            "accessibleName": "Save Layout",
            "text": "Save Layout",
            "placeholder": None,
            "inputType": None,
            "landmark": None,
            "xpath": "/html/body/div/button[1]",
            "bbox": {"x": 0, "y": 0, "width": 100, "height": 30},
        },
        {
            "tag": "button",
            "id": None,
            "testId": "btn-cancel",
            "role": "button",
            "ariaLabel": None,
            "accessibleName": "Cancel",
            "text": "Cancel",
            "placeholder": None,
            "inputType": None,
            "landmark": None,
            "xpath": "/html/body/div/button[2]",
            "bbox": {"x": 110, "y": 0, "width": 80, "height": 30},
        },
    ])
    repair = LocatorRepair()  # deterministic
    result = repair.heal(page, fp, semantic_label="click_btn_save_layout")
    assert result.locator is not None
    assert result.confidence == "high"
    assert result.new_fingerprint is not None
    assert result.new_fingerprint.test_id == "btn-save-layout"
    assert result.backend == "deterministic"


def test_deterministic_medium_match_via_accessible_name() -> None:
    """Original had no testid (rare on legacy portals); role + name
    carry the match. Score should land in medium band."""
    fp = ElementFingerprint(
        test_id=None,  # no testid recorded
        accessible_name="Save Layout",
        role="button",
        tag="button",
    )
    page = _FakePage([
        {
            "tag": "button",
            "id": None,
            "testId": None,
            "role": "button",
            "ariaLabel": None,
            "accessibleName": "Save Layout as Draft",  # similar but not identical
            "text": "Save Layout as Draft",
            "placeholder": None,
            "inputType": None,
            "landmark": None,
            "xpath": "/html/body/div/button[1]",
            "bbox": {},
        },
    ])
    repair = LocatorRepair()
    result = repair.heal(page, fp, semantic_label="click_btn_save_layout")
    # Score should clear the refuse threshold; band is high or medium.
    assert result.confidence in ("high", "medium")
    assert result.locator is not None


def test_deterministic_drift_with_testid_renamed_refuses() -> None:
    """Operator-style scenario: testid renamed entirely AND accessible
    name unchanged. The strong testid signal vanishing should pull the
    score below high but the strong name match should keep it usable.
    Specifically check we don't mistakenly mark low (refuse).
    """
    fp = ElementFingerprint(
        test_id="btn-save-layout",
        accessible_name="Save Layout",
        text="Save Layout",
        role="button",
        tag="button",
        landmark="page-curation",
    )
    page = _FakePage([
        {
            "tag": "button",
            "id": None,
            "testId": "btn-save-layout-v2",  # renamed; no match
            "role": "button",
            "ariaLabel": None,
            "accessibleName": "Save Layout",  # exact name match
            "text": "Save Layout",
            "placeholder": None,
            "inputType": None,
            "landmark": "page-curation",  # same landmark
            "xpath": "/html/body/div/button[1]",
            "bbox": {},
        },
    ])
    repair = LocatorRepair()
    result = repair.heal(page, fp, semantic_label="click_btn_save_layout")
    # Caller should get something it can act on (not 'low' refuse).
    assert result.locator is not None
    assert result.confidence in ("high", "medium")


def test_deterministic_low_refused_for_unrelated_candidate() -> None:
    fp = ElementFingerprint(
        test_id="btn-save-layout",
        accessible_name="Save Layout",
        role="button",
        tag="button",
    )
    page = _FakePage([
        {
            "tag": "a",
            "id": None,
            "testId": None,
            "role": "link",
            "ariaLabel": None,
            "accessibleName": "Help docs",
            "text": "Help docs",
            "placeholder": None,
            "inputType": None,
            "landmark": None,
            "xpath": "/html/body/a",
            "bbox": {},
        },
    ])
    repair = LocatorRepair()
    result = repair.heal(page, fp, semantic_label="click_btn_save_layout")
    # Below threshold — refuse with locator=None
    assert result.locator is None
    assert result.confidence == "low"


def test_deterministic_empty_page() -> None:
    fp = ElementFingerprint(test_id="anything", role="button")
    page = _FakePage([])
    repair = LocatorRepair()
    result = repair.heal(page, fp)
    assert result.locator is None
    assert result.confidence == "low"


# ---------------------------------------------------------------------------
# LLM backend (with injected fake)
# ---------------------------------------------------------------------------


def test_llm_backend_accepts_pick() -> None:
    fp = ElementFingerprint(
        test_id="btn-save-layout",
        accessible_name="Save Layout",
        role="button",
        tag="button",
    )
    candidates = [
        {
            "tag": "button",
            "id": None,
            "testId": "btn-save-draft",
            "role": "button",
            "ariaLabel": None,
            "accessibleName": "Save Draft",
            "text": "Save Draft",
            "placeholder": None,
            "inputType": None,
            "landmark": None,
            "xpath": "/x/0",
            "bbox": {},
        },
        {
            "tag": "button",
            "id": None,
            "testId": "btn-publish",
            "role": "button",
            "ariaLabel": None,
            "accessibleName": "Publish",
            "text": "Publish",
            "placeholder": None,
            "inputType": None,
            "landmark": None,
            "xpath": "/x/1",
            "bbox": {},
        },
    ]
    page = _FakePage(candidates)

    # Fake complete_structured returns a confident pick of index 0.
    class _FakeOut:
        element_index = 0
        action = "click"
        confidence = "high"
        reason = "Save Draft is the closest match for the recorded Save Layout intent"

    def fake_complete_structured(client, *, messages, response_model, **_kw):
        # Validate the prompt mentions the intent
        sys_msg = messages[0].content
        user_msg = messages[1].content
        assert "operator" in sys_msg.lower() or "operator" in user_msg.lower()
        assert "btn-save-layout" in user_msg
        return _FakeOut()

    class _FakeMessage:
        def __init__(self, role: str, content: str) -> None:
            self.role = role
            self.content = content

    repair = LocatorRepair(
        client=object(),  # any non-None
        model="fake-model",
        complete_structured=fake_complete_structured,
        Message=_FakeMessage,
    )
    result = repair.heal(page, fp, semantic_label="click_btn_save_layout")
    assert result.locator is not None
    assert result.confidence == "high"
    assert result.new_fingerprint is not None
    assert result.new_fingerprint.test_id == "btn-save-draft"
    assert result.backend == "llm"


def test_llm_backend_refuses() -> None:
    fp = ElementFingerprint(test_id="anything", role="button", accessible_name="X")
    page = _FakePage([
        {
            "tag": "div", "id": None, "testId": None, "role": None,
            "ariaLabel": None, "accessibleName": "irrelevant",
            "text": "irrelevant", "placeholder": None, "inputType": None,
            "landmark": None, "xpath": "/x", "bbox": {},
        }
    ])

    class _FakeOut:
        element_index = -1
        action = "click"
        confidence = "low"
        reason = "no candidate matches"

    def fake_complete_structured(client, *, messages, response_model, **_kw):
        return _FakeOut()

    class _FakeMessage:
        def __init__(self, role: str, content: str) -> None:
            self.role, self.content = role, content

    repair = LocatorRepair(
        client=object(),
        complete_structured=fake_complete_structured,
        Message=_FakeMessage,
    )
    result = repair.heal(page, fp)
    assert result.locator is None
    assert result.confidence == "low"
    assert result.backend == "llm"
