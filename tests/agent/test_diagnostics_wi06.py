"""WI-06: structured diagnostics replace silent failures.

Verifies each previously-silent failure site (teach.py + skill_runner.py
+ executor_real.py) now emits a Diagnostic with a stable code.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pilot.agent.executor_real import RealExecutor, RealExecutorConfig
from pilot.agent.schemas.protocol import DiagnosticEvent
from pilot.models import Diagnostic


# ---------------------------------------------------------------------------
# DiagnosticEvent + Diagnostic round-trip
# ---------------------------------------------------------------------------


def test_diagnostic_model_roundtrip():
    d = Diagnostic(
        code="runner.watcher_install_failed",
        context={"exc_type": "JSError", "step_index": 4},
        recoverable=True,
        level="warn",
    )
    dumped = d.model_dump_json()
    restored = Diagnostic.model_validate_json(dumped)
    assert restored.code == "runner.watcher_install_failed"
    assert restored.context["step_index"] == 4
    assert restored.recoverable is True
    assert restored.level == "warn"


def test_diagnostic_event_schema():
    ev = DiagnosticEvent(  # type: ignore[call-arg]
        task_id="task-1",
        code="executor.hint_persist_failed",
        level="error",
        recoverable=False,
        context={"sidecar_path": "/tmp/x.hints.json"},
        step_index=2,
    )
    payload = ev.model_dump()
    assert payload["type"] == "diagnostic"
    assert payload["code"] == "executor.hint_persist_failed"
    assert payload["recoverable"] is False
    assert payload["level"] == "error"


# ---------------------------------------------------------------------------
# RealExecutor diagnostic accumulation
# ---------------------------------------------------------------------------


def _make_executor(tmp_path: Path) -> RealExecutor:
    cfg = RealExecutorConfig(
        skills_dir=tmp_path / "skills",
        sessions_dir=tmp_path / "sessions",
    )
    cfg.skills_dir.mkdir()
    cfg.sessions_dir.mkdir()
    return RealExecutor(cfg)


def test_record_and_drain_diagnostics(tmp_path: Path):
    ex = _make_executor(tmp_path)
    assert ex.diagnostics == []
    ex._record_diagnostic(
        "executor.alternate_persist_failed",
        level="error",
        recoverable=False,
        skill_path="/x",
    )
    ex._record_diagnostic(
        "executor.hint_persist_failed",
        level="warn",
        recoverable=True,
    )
    drained = ex.drain_diagnostics()
    assert len(drained) == 2
    assert drained[0].code == "executor.alternate_persist_failed"
    assert drained[1].code == "executor.hint_persist_failed"
    # Drain is one-shot.
    assert ex.diagnostics == []


def test_hint_persist_diagnostic_on_load_failure(tmp_path: Path):
    """A malformed hints sidecar should produce a structured diagnostic
    -- previously the silent return-empty hid the failure."""
    ex = _make_executor(tmp_path)
    # Place a malformed sidecar.
    skill_path = tmp_path / "skills" / "demo.json"
    skill_path.write_text('{"name":"demo","steps":[]}', encoding="utf-8")
    hints = skill_path.with_suffix(".hints.json")
    hints.write_text("{this is not valid json", encoding="utf-8")
    # Force the load.
    ex._load_hints_sidecar(skill_path)
    diagnostics = ex.drain_diagnostics()
    codes = [d.code for d in diagnostics]
    assert "executor.hint_sidecar_parse_failed" in codes


def test_alternate_persist_diagnostic_on_parse_failure(tmp_path: Path):
    ex = _make_executor(tmp_path)
    skill_path = tmp_path / "skills" / "broken.json"
    skill_path.write_text("not json at all", encoding="utf-8")
    n = ex._persist_alternates_to_skill(
        skill_path,
        [(0, {"new_fingerprint": {"test_id": "foo"}})],
    )
    assert n == 0
    diagnostics = ex.drain_diagnostics()
    codes = [d.code for d in diagnostics]
    assert "executor.alternate_persist_load_failed" in codes


def test_hint_persist_diagnostic_when_skill_missing(tmp_path: Path):
    ex = _make_executor(tmp_path)
    ok = ex.persist_disambiguation_hint(
        "nonexistent", 0, {"chosen_test_id": "foo"}
    )
    assert ok is False
    diagnostics = ex.drain_diagnostics()
    codes = [d.code for d in diagnostics]
    assert "executor.hint_persist_skill_not_found" in codes


# ---------------------------------------------------------------------------
# SkillRunner diagnostic (synthetic without a live browser)
# ---------------------------------------------------------------------------


def test_runner_diagnostic_records_and_audits():
    """SkillRunner._diagnostic appends to self.diagnostics + writes via
    the audit logger. We exercise the path without a real browser by
    constructing a minimal runner state and calling the method
    directly."""
    from pilot.skill_runner import SkillRunner
    from pilot.skill_models import Skill, SkillStep

    skill = Skill(name="t", steps=[SkillStep(index=0, action="click")])

    # Inject mocks for the BrowserSession + audit so we don't need
    # Chrome / sync_playwright spun up.
    runner = object.__new__(SkillRunner)  # bypass __init__
    runner.skill = skill
    runner.diagnostics = []
    runner.audit = MagicMock()
    # Provide _diag dict so _diagnostic can look up step_index.
    runner._diag = {"step_index": 3}

    SkillRunner._diagnostic(  # type: ignore[arg-type]
        runner,
        "runner.watcher_install_failed",
        level="warn",
        recoverable=True,
        exc_type="JSHandle",
    )
    SkillRunner._diagnostic(
        runner,
        "runner.set_selection_search_failed",
        level="warn",
        item="sports",
    )
    assert len(runner.diagnostics) == 2
    assert runner.diagnostics[0].code == "runner.watcher_install_failed"
    assert runner.audit.log.call_count == 2
    # Check the audit log received the structured diagnostic data.
    first_call = runner.audit.log.call_args_list[0]
    assert first_call.kwargs["data"]["diagnostic_code"] == "runner.watcher_install_failed"
    assert first_call.kwargs["data"]["step_index"] == 3
