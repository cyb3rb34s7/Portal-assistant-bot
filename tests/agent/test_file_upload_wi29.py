"""WI-29: strengthen file upload modeling.

Plan acceptance check:
  - Replay with a DIFFERENT file path succeeds when constraints match.
  - Replay fails BEFORE page mutation when file is missing or MIME/
    extension doesn't match the recorded accept attribute.

Tests cover the deterministic pipeline (no live browser):
  - Schema roundtrip: FileSpec + FileMetadata persist.
  - Annotator: file_selected event with accept attr -> upload step
    with file_spec populated + ParamConstraints derived from accept.
  - Annotator: multiple=True flips the SkillParam type to
    file_path_list.
  - Runner: _do_upload rejects missing files with
    error_kind=file_validation_failed BEFORE locator resolution.
  - Runner: _validate_against_accept allows recorded extension but
    rejects extension not in accept list.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock

from pilot.annotate import build_skill
from pilot.skill_models import (
    ElementFingerprint,
    FileMetadata,
    FileSpec,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import SkillRunner


def _file_selected(
    event_id: str,
    file_name: str,
    accept: str = ".csv,text/csv",
    multiple: bool = False,
    test_id: str = "input-csv-file",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="file_selected",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="input",
            input_type="file",
            control_kind="file_input",
            value_kind="file",
            accept=accept,
            multiple=multiple,
        ),
        file_name=file_name,
        file_metadata=[
            FileMetadata(
                name=file_name,
                size=1024,
                mime="text/csv",
                ext=".csv",
            ),
        ],
        page_url="http://x",
        event_id=event_id,
        sequence=1,
        source="user_file_selected",
    )


# ----- schema --------------------------------------------------------------


def test_file_spec_roundtrip() -> None:
    spec = FileSpec(
        original_name="contents.csv",
        extension=".csv",
        mime_hint="text/csv",
        size=2048,
        accept_attribute=".csv,text/csv",
        multiple_flag=False,
        recorded_files=[
            FileMetadata(
                name="contents.csv",
                size=2048,
                mime="text/csv",
                ext=".csv",
            ),
        ],
    )
    step = SkillStep(
        index=0,
        action="upload",
        fingerprint=ElementFingerprint(test_id="input-csv-file"),
        file_spec=spec,
    )
    skill = Skill(name="t", steps=[step])
    restored = Skill.model_validate_json(skill.model_dump_json())
    rs = restored.steps[0].file_spec
    assert rs is not None
    assert rs.original_name == "contents.csv"
    assert rs.extension == ".csv"
    assert rs.mime_hint == "text/csv"
    assert rs.accept_attribute == ".csv,text/csv"
    assert rs.multiple_flag is False
    assert len(rs.recorded_files) == 1
    assert rs.recorded_files[0].name == "contents.csv"


# ----- annotator -----------------------------------------------------------


def test_annotator_emits_file_spec_with_accept_constraints() -> None:
    """File input with accept=.csv -> upload step has file_spec and
    SkillParam constraints derived from accept."""
    events = [
        _file_selected("e1", "contents.csv", accept=".csv,text/csv"),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    upload_steps = [s for s in skill.steps if s.action == "upload"]
    assert len(upload_steps) == 1
    s = upload_steps[0]
    # file_spec populated.
    assert s.file_spec is not None
    assert s.file_spec.accept_attribute == ".csv,text/csv"
    assert s.file_spec.original_name == "contents.csv"
    assert s.file_spec.multiple_flag is False
    # SkillParam declared with ParamConstraints derived from accept.
    binding = s.param_binding
    assert binding is not None
    params_by_name = {p.name: p for p in skill.params}
    p = params_by_name[binding.name]
    assert p.type == "file_path"
    assert p.constraints is not None
    assert p.constraints.extensions == [".csv"]
    assert p.constraints.mime_types == ["text/csv"]


def test_annotator_multiple_flag_upgrades_to_file_path_list() -> None:
    """File input with multiple=True -> SkillParam.type = file_path_list."""
    events = [
        _file_selected(
            "e1",
            "first.jpg",
            accept="image/*",
            multiple=True,
        ),
    ]
    skill = build_skill(skill_name="t", events=events, auto=True)
    upload_steps = [s for s in skill.steps if s.action == "upload"]
    assert len(upload_steps) == 1
    s = upload_steps[0]
    assert s.file_spec is not None
    assert s.file_spec.multiple_flag is True
    binding = s.param_binding
    assert binding is not None
    params_by_name = {p.name: p for p in skill.params}
    p = params_by_name[binding.name]
    assert p.type == "file_path_list"
    # accept=image/* -> mime_types=['image/']
    assert p.constraints is not None
    assert p.constraints.mime_types == ["image/"]


def test_annotator_falls_back_when_no_file_metadata() -> None:
    """Legacy trace with only file_name and no file_metadata still
    produces a FileSpec with original_name + extension derived."""
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="file_selected",
        fingerprint=ElementFingerprint(
            test_id="input-csv-file",
            control_kind="file_input",
            value_kind="file",
            accept=".csv",
        ),
        file_name="legacy.csv",
        page_url="http://x",
        event_id="e1",
        sequence=1,
    )
    skill = build_skill(skill_name="t", events=[ev], auto=True)
    s = [s for s in skill.steps if s.action == "upload"][0]
    assert s.file_spec is not None
    assert s.file_spec.original_name == "legacy.csv"
    assert s.file_spec.extension == ".csv"
    # recorded_files fell back to one entry derived from file_name.
    assert len(s.file_spec.recorded_files) == 1


# ----- runner validation --------------------------------------------------


def test_runner_validates_against_accept_extension_match() -> None:
    """Path with matching extension passes."""
    sess = MagicMock()
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = sess
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(b"a,b,c\n1,2,3")
        tmp = f.name
    try:
        ok, reason = runner._validate_against_accept(
            [tmp], ".csv,text/csv"
        )
        assert ok is True
        assert reason is None
    finally:
        os.unlink(tmp)


def test_runner_validates_against_accept_extension_mismatch() -> None:
    """Path with non-matching extension/mime is rejected."""
    sess = MagicMock()
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = sess
    with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
        f.write(b"\x00")
        tmp = f.name
    try:
        ok, reason = runner._validate_against_accept(
            [tmp], ".csv,text/csv"
        )
        assert ok is False
        assert reason is not None
        assert ".exe" in reason
    finally:
        os.unlink(tmp)


def test_runner_validates_mime_glob_image_prefix() -> None:
    """accept=image/* should match anything mime starts with image/."""
    sess = MagicMock()
    runner = SkillRunner.__new__(SkillRunner)
    runner.session = sess
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"\xff\xd8\xff")
        tmp = f.name
    try:
        ok, reason = runner._validate_against_accept([tmp], "image/*")
        assert ok is True
    finally:
        os.unlink(tmp)


def test_runner_do_upload_rejects_missing_file_before_locator() -> None:
    """The acceptance bar: a non-existent path returns
    file_validation_failed BEFORE locator resolution / page mutation."""
    sess = MagicMock()
    page = MagicMock()
    sess.page = page

    runner = SkillRunner.__new__(SkillRunner)
    runner.session = sess
    runner.params = {"csv_file": "/nonexistent/path/does_not_exist.csv"}
    # Minimal SkillStep with file_path binding.
    from pilot.skill_models import ParamBinding
    step = SkillStep(
        index=0,
        action="upload",
        fingerprint=ElementFingerprint(test_id="input-csv-file"),
        param_binding=ParamBinding(
            name="csv_file", type="file_path", mode="whole"
        ),
        file_spec=FileSpec(
            original_name="contents.csv",
            extension=".csv",
            accept_attribute=".csv",
            multiple_flag=False,
        ),
    )
    result, level = runner._do_upload(
        step, "/nonexistent/path/does_not_exist.csv"
    )
    assert result.success is False
    assert result.error_kind == "file_validation_failed"
    # set_input_files must NOT have been called -- the validation
    # happened upstream of locator resolution.
    page.locator.assert_not_called()
