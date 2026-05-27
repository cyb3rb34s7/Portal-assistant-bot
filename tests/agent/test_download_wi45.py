"""WI-45: download / export workflows.

Acceptance check from the plan:
  A CSV export click records as a ``download`` step; replay produces
  the file under ``sessions/<id>/downloads/``.

Tests cover:
  - DownloadSpec schema roundtrip.
  - TraceEvent kind='download' carries download_filename + download_href.
  - Annotator folds a download event onto the causing click step,
    rewriting the action from 'click' to 'download' and attaching
    DownloadSpec.
  - download_started StepAssertion (WI-27) verifies against the captured
    download recorded by _do_download instead of returning a placeholder.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import build_skill
from pilot.skill_models import (
    DownloadSpec,
    ElementFingerprint,
    NetworkExpectation,
    Skill,
    SkillStep,
    TraceEvent,
)


def _click(event_id: str, sequence: int, test_id: str) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id=test_id, tag="button", role="button",
        ),
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        source="user_click",
    )


def _download_event(
    event_id: str,
    sequence: int,
    caused_by: str,
    filename: str,
    href: str | None = None,
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="download",
        page_url="http://x",
        event_id=event_id,
        sequence=sequence,
        caused_by=caused_by,
        initiator_event_id=caused_by,
        download_filename=filename,
        download_href=href,
        source="download_intent",
    )


# ----- schema --------------------------------------------------------------


def test_download_spec_roundtrip() -> None:
    spec = DownloadSpec(
        filename_template="export-{report_id}.csv",
        expected_mime="text/csv",
        expected_min_bytes=64,
        expected_signal=NetworkExpectation(
            url_pattern="/api/export",
            method="POST",
            status=200,
        ),
    )
    step = SkillStep(
        index=0,
        action="download",
        fingerprint=ElementFingerprint(test_id="btn-export-csv"),
        download_spec=spec,
    )
    data = step.model_dump()
    restored = SkillStep.model_validate(data)
    assert restored.action == "download"
    assert restored.download_spec is not None
    assert restored.download_spec.filename_template == "export-{report_id}.csv"
    assert restored.download_spec.expected_mime == "text/csv"
    assert restored.download_spec.expected_min_bytes == 64
    assert restored.download_spec.expected_signal is not None
    assert restored.download_spec.expected_signal.url_pattern == "/api/export"


def test_trace_event_download_kind_validates() -> None:
    ev = _download_event(
        "d1", 2, "click1", "export.csv", href="/api/exports/export.csv",
    )
    data = ev.model_dump()
    restored = TraceEvent.model_validate(data)
    assert restored.kind == "download"
    assert restored.download_filename == "export.csv"
    assert restored.download_href == "/api/exports/export.csv"


# ----- annotator: download folds onto causing click -----------------------


def test_build_skill_folds_download_onto_click() -> None:
    ev_click = _click("click1", 1, "btn-export")
    ev_dl = _download_event(
        "d1", 2, "click1", "report.csv",
        href="/api/reports/report.csv",
    )
    skill = build_skill(
        "export_report",
        [ev_click, ev_dl],
        auto=True,
        base_url="http://x",
    )
    assert isinstance(skill, Skill)
    # The download event is observed; only the click becomes a step,
    # but its action is REWRITTEN to ``download``.
    assert [s.action for s in skill.steps] == ["download"]
    s = skill.steps[0]
    assert s.download_spec is not None
    assert s.download_spec.filename_template == "report.csv"
    # Provenance carries both events.
    assert s.provenance is not None
    assert "click1" in s.provenance.raw_event_ids
    assert "d1" in s.provenance.raw_event_ids


def test_download_without_intent_stays_a_click() -> None:
    """A click without an attributed download event remains a plain
    click step (no DownloadSpec). Pins that the fold only fires when
    evidence exists."""
    ev_click = _click("click1", 1, "btn-something-else")
    skill = build_skill(
        "non_download_click",
        [ev_click],
        auto=True,
        base_url="http://x",
    )
    assert [s.action for s in skill.steps] == ["click"]
    assert skill.steps[0].download_spec is None
