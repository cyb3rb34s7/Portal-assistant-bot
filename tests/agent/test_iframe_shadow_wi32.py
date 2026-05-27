"""WI-32: iframe + shadow DOM traversal.

Plan acceptance check (unit-level; live-portal verification deferred
to WI-50):
  Element inside a same-origin iframe AND element inside a shadow
  root can be RECORDED + REPLAYED via the schema's frame_chain /
  shadow_path metadata.

Tests cover:
  - FrameStep schema validation (iframe step requires selector;
    shadow step requires host_selector).
  - ElementFingerprint roundtrip with frame_chain + shadow_path.
  - Runner's _scope_for_fingerprint walks frame_chain into
    page.frame_locator(selector) for iframe steps.
  - Legacy frame_path (pre-WI-32) is honored when frame_chain is
    empty.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from pilot.skill_models import (
    ElementFingerprint,
    FrameStep,
    Skill,
    SkillStep,
)
from pilot.skill_runner import SkillRunner


# ----- schema --------------------------------------------------------------


def test_frame_step_iframe_requires_selector() -> None:
    """A FrameStep(kind='iframe') without selector is rejected."""
    with pytest.raises(ValidationError):
        FrameStep(kind="iframe")
    # Valid: with selector.
    step = FrameStep(kind="iframe", selector='iframe[name="preview"]')
    assert step.selector == 'iframe[name="preview"]'


def test_frame_step_shadow_requires_host_selector() -> None:
    """A FrameStep(kind='shadow') without host_selector is rejected."""
    with pytest.raises(ValidationError):
        FrameStep(kind="shadow")
    # Valid: with host_selector.
    step = FrameStep(kind="shadow", host_selector=".ds-host")
    assert step.host_selector == ".ds-host"


def test_fingerprint_with_frame_chain_roundtrips() -> None:
    """ElementFingerprint with frame_chain + shadow_path persists
    through JSON roundtrip."""
    fp = ElementFingerprint(
        test_id="btn-inner",
        frame_chain=[
            FrameStep(kind="iframe", selector='iframe[name="preview"]'),
            FrameStep(kind="shadow", host_selector=".ds-host"),
        ],
        shadow_path=[".ds-host"],
    )
    step = SkillStep(index=0, action="click", fingerprint=fp)
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rfp = restored.steps[0].fingerprint
    assert rfp is not None
    assert len(rfp.frame_chain) == 2
    assert rfp.frame_chain[0].kind == "iframe"
    assert rfp.frame_chain[0].selector == 'iframe[name="preview"]'
    assert rfp.frame_chain[1].kind == "shadow"
    assert rfp.frame_chain[1].host_selector == ".ds-host"
    assert rfp.shadow_path == [".ds-host"]


def test_fingerprint_legacy_frame_path_field_preserved() -> None:
    """Pre-WI-32 fingerprints with frame_path (list[str]) still load."""
    fp = ElementFingerprint(
        test_id="btn-legacy",
        frame_path=["iframe[name='preview']"],
        in_shadow_root=True,
    )
    step = SkillStep(index=0, action="click", fingerprint=fp)
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rfp = restored.steps[0].fingerprint
    assert rfp is not None
    assert rfp.frame_path == ["iframe[name='preview']"]
    assert rfp.in_shadow_root is True


# ----- runner traversal ---------------------------------------------------


def test_scope_for_fingerprint_no_traversal_when_no_chain() -> None:
    """Empty frame_chain + empty frame_path -> returns page unchanged."""
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    page = MagicMock()
    fp = ElementFingerprint(test_id="btn")
    scope = runner._scope_for_fingerprint(page, fp)
    assert scope is page
    page.frame_locator.assert_not_called()


def test_scope_for_fingerprint_descends_iframe_chain() -> None:
    """frame_chain with one iframe step -> page.frame_locator called."""
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    page = MagicMock()
    inner = MagicMock()
    page.frame_locator.return_value = inner
    fp = ElementFingerprint(
        test_id="btn-inside-iframe",
        frame_chain=[
            FrameStep(kind="iframe", selector='iframe[name="preview"]'),
        ],
    )
    scope = runner._scope_for_fingerprint(page, fp)
    page.frame_locator.assert_called_once_with('iframe[name="preview"]')
    assert scope is inner


def test_scope_for_fingerprint_descends_multiple_iframes() -> None:
    """Nested iframes: chain[0] then chain[1]."""
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    page = MagicMock()
    inner1 = MagicMock()
    inner2 = MagicMock()
    page.frame_locator.return_value = inner1
    inner1.frame_locator.return_value = inner2
    fp = ElementFingerprint(
        test_id="btn-double-nested",
        frame_chain=[
            FrameStep(kind="iframe", selector='iframe[name="outer"]'),
            FrameStep(kind="iframe", selector='iframe[name="inner"]'),
        ],
    )
    scope = runner._scope_for_fingerprint(page, fp)
    page.frame_locator.assert_called_once_with('iframe[name="outer"]')
    inner1.frame_locator.assert_called_once_with('iframe[name="inner"]')
    assert scope is inner2


def test_scope_for_fingerprint_legacy_frame_path_fallback() -> None:
    """Pre-WI-32 frame_path (list[str]) is honored when frame_chain is
    empty -- each entry treated as an iframe selector."""
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    page = MagicMock()
    inner = MagicMock()
    page.frame_locator.return_value = inner
    fp = ElementFingerprint(
        test_id="legacy-btn",
        frame_path=['iframe[name="legacy"]'],
    )
    scope = runner._scope_for_fingerprint(page, fp)
    page.frame_locator.assert_called_once_with('iframe[name="legacy"]')
    assert scope is inner


def test_scope_for_fingerprint_shadow_step_does_not_change_scope() -> None:
    """Playwright auto-pierces OPEN shadow roots; the shadow step in
    frame_chain is informational. Scope stays at page."""
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = MagicMock()
    runner._diagnostic = MagicMock()
    page = MagicMock()
    fp = ElementFingerprint(
        test_id="btn-inside-shadow",
        frame_chain=[FrameStep(kind="shadow", host_selector=".ds-host")],
        shadow_path=[".ds-host"],
    )
    scope = runner._scope_for_fingerprint(page, fp)
    # No frame_locator call (shadow, not iframe).
    page.frame_locator.assert_not_called()
    # The shadow step is logged as a diagnostic for audit.
    runner._diagnostic.assert_called()
    assert scope is page
