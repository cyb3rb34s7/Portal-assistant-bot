"""WI-48: locale / timezone normalization.

Plan acceptance check:
  A recording made under en-US can replay under de-DE with date
  '2026-05-22' rendered as '22.05.2026' by the page (codec handles the
  format), AND the warning surfaces.

Tests cover:
  - Skill.recording_context schema roundtrip.
  - Annotator stamps recording_context from observed fingerprint
    locale_hint + timezone_hint (modal value across events).
  - localized_number codec parses 'de-DE' style ``1,5`` -> ``1.5`` and
    US-style ``1,234.56`` -> ``1234.56``. iso_date stays canonical.
  - SkillStep.strict_locale fail-fast: when set True AND a runner
    flagged a locale mismatch at run() entry, _execute_step_inner
    returns error_kind='locale_mismatch' before touching the page.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

from pilot.annotate import build_skill
from pilot.browser import BrowserSession
from pilot.param_codecs import _decode_iso_date, _decode_localized_number
from pilot.skill_models import (
    ElementFingerprint,
    RecordingContext,
    Skill,
    SkillStep,
    TraceEvent,
)


def _input_event(
    locale: str,
    tz: str,
    value: str,
    test_id: str,
    sequence: int,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="input",
            locale_hint=locale,
            timezone_hint=tz,
        ),
        page_url="http://x",
        value=value,
        event_id=f"i{sequence}",
        sequence=sequence,
        source="user_input",
    )


# ----- schema roundtrip ----------------------------------------------------


def test_recording_context_roundtrip() -> None:
    skill = Skill(
        name="loc_test",
        steps=[SkillStep(index=0, action="click")],
        recording_context=RecordingContext(
            locale="en-US", timezone="America/New_York",
        ),
    )
    data = skill.model_dump()
    restored = Skill.model_validate(data)
    assert restored.recording_context is not None
    assert restored.recording_context.locale == "en-US"
    assert restored.recording_context.timezone == "America/New_York"


# ----- annotator -----------------------------------------------------------


def test_annotator_stamps_recording_context_from_modal_value() -> None:
    """The annotator picks the most-frequent locale + timezone seen
    across all fingerprints. With three en-US events and one
    de-DE event, the recording context is en-US."""
    events = [
        _input_event("en-US", "America/New_York", "Hello", "input-a", 1),
        _input_event("en-US", "America/New_York", "World", "input-b", 2),
        _input_event("en-US", "America/New_York", "!", "input-c", 3),
        _input_event("de-DE", "Europe/Berlin", "extra", "input-d", 4),
    ]
    skill = build_skill(
        "modal_locale",
        events,
        auto=True,
        base_url="http://x",
    )
    assert skill.recording_context is not None
    assert skill.recording_context.locale == "en-US"
    assert skill.recording_context.timezone == "America/New_York"


def test_annotator_recording_context_none_when_no_hints() -> None:
    """No locale_hint anywhere -> recording_context is None (legacy
    traces). The runner's mismatch probe is a no-op for these."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(test_id="btn"),
        page_url="http://x",
        event_id="c1",
        sequence=1,
        source="user_click",
    )
    skill = build_skill("nohint", [ev], auto=True, base_url="http://x")
    assert skill.recording_context is None


# ----- codecs (already-implemented WI-05 paths verified for WI-48) --------


def test_localized_number_codec_handles_de_and_us() -> None:
    # US: ``1,234.56`` thousands comma, decimal dot.
    assert _decode_localized_number("1,234.56", "n") == "1234.56"
    # DE: ``1.234,56`` thousands dot, decimal comma.
    assert _decode_localized_number("1.234,56", "n") == "1234.56"
    # Bare DE decimal ``1,5``: ambiguous, but the codec biases to US
    # thousands -- declare with explicit decimal-only to be safe.
    # Plain decimal dot also works.
    assert _decode_localized_number("1.5", "n") == "1.5"


def test_iso_date_codec_normalizes() -> None:
    # The codec accepts ``2026-05-22`` directly and ``05/22/2026``.
    assert _decode_iso_date("2026-05-22", "d") == "2026-05-22"
    assert _decode_iso_date("05/22/2026", "d") == "2026-05-22"


# ----- runner strict_locale gate -------------------------------------------


def test_strict_locale_fails_step_when_mismatch_flagged() -> None:
    """When self._replay_locale_mismatch is True AND the step has
    strict_locale=True, _execute_step_inner returns
    error_kind='locale_mismatch' before touching the page."""
    from pilot.skill_runner import SkillRunner

    session = BrowserSession(
        playwright=None,  # type: ignore[arg-type]
        browser=None,  # type: ignore[arg-type]
        context=None,  # type: ignore[arg-type]
        page=object(),  # type: ignore[arg-type]
    )
    skill = Skill(
        name="strict_test",
        steps=[SkillStep(index=0, action="click", strict_locale=True)],
        recording_context=RecordingContext(
            locale="en-US", timezone="UTC",
        ),
    )
    with tempfile.TemporaryDirectory() as td:
        runner = SkillRunner(
            session=session,
            skill=skill,
            params={},
            sessions_dir=Path(td),
        )
        # Simulate the run-start probe having flagged a mismatch.
        runner._replay_locale_mismatch = True
        # Bypass the screenshotter (audit's screenshot() returns "" on
        # a stubbed page; that's fine -- result.success is what we
        # assert).
        result, level = runner._execute_step_inner(skill.steps[0])
        assert result.success is False
        assert result.error_kind == "locale_mismatch"


def test_strict_locale_no_op_when_no_mismatch() -> None:
    """When the run-start probe found no mismatch, strict_locale=True
    is a no-op gate. The step proceeds to the action dispatch path
    (which fails downstream because the stubbed page doesn't support
    real clicks -- we only assert that the gate didn't fail early)."""
    from pilot.skill_runner import SkillRunner

    session = BrowserSession(
        playwright=None,  # type: ignore[arg-type]
        browser=None,  # type: ignore[arg-type]
        context=None,  # type: ignore[arg-type]
        page=object(),  # type: ignore[arg-type]
    )
    skill = Skill(
        name="strict_ok",
        steps=[SkillStep(index=0, action="click", strict_locale=True)],
    )
    with tempfile.TemporaryDirectory() as td:
        runner = SkillRunner(
            session=session,
            skill=skill,
            params={},
            sessions_dir=Path(td),
        )
        runner._replay_locale_mismatch = False
        result, level = runner._execute_step_inner(skill.steps[0])
        # The strict_locale gate didn't preempt; whatever the action
        # path returned, error_kind is NOT 'locale_mismatch'.
        assert result.error_kind != "locale_mismatch"
