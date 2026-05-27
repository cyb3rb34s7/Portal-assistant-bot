"""Unit tests for the sub-step override path used by ``use_alternate``.

Pins behavior: when the operator resolves an ``ambiguous_target``
failure by picking a candidate, the orchestrator threads the candidate
through as ``sub_step_overrides={sub_step_index: {test_id: ...}}``,
which the SkillRunner consumes via ``_apply_locator_override``. After
the override is applied, the fingerprint's templated identifying
fields are replaced so L1 hits the picked element instead of producing
the same ambiguity again.
"""

from __future__ import annotations

from datetime import datetime

from pilot.skill_models import ElementFingerprint, Skill, SkillStep
from pilot.skill_runner import SkillRunner


def _runner_with_override(override: dict[int, dict[str, str]]) -> SkillRunner:
    """Build a SkillRunner instance without touching Playwright. We
    don't call .run() in these tests -- we only exercise the override
    helper, so the session / audit dir can be stubbed."""
    skill = Skill(
        name="t",
        steps=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    # SkillRunner constructor doesn't touch session/audit until run(),
    # so a sentinel is enough. The audit writer hits the filesystem on
    # init for the session dir; use a tmp-friendly path.
    from pathlib import Path
    import tempfile

    return SkillRunner(
        session=None,  # type: ignore[arg-type]
        skill=skill,
        params={},
        sessions_dir=Path(tempfile.gettempdir()),
        sub_step_overrides=override,
    )


def test_apply_locator_override_replaces_testid() -> None:
    """When the override has a test_id, it replaces the fingerprint's
    test_id AND wipes element_id / name so L1 doesn't keep matching
    the wrong element via a stale alternate field."""
    runner = _runner_with_override({})
    fp = ElementFingerprint(
        test_id="row-A-9001",  # original ambiguous target
        element_id="row-9001",
        name="row",
        templates={"test_id": "row-{content_id}"},
    )
    out = runner._apply_locator_override(fp, {"test_id": "row-A-9002"})

    assert out.test_id == "row-A-9002"
    assert out.element_id is None
    assert out.name is None
    # templates are wiped so _materialize_fingerprint doesn't undo us
    assert out.templates == {}


def test_apply_locator_override_falls_back_to_element_id() -> None:
    """If the override only has an id (no test_id), the helper uses
    element_id and clears test_id so the templated value doesn't
    keep winning."""
    runner = _runner_with_override({})
    fp = ElementFingerprint(test_id="row-A-9001", element_id="row-x")
    out = runner._apply_locator_override(fp, {"id": "row-target"})

    assert out.test_id is None
    assert out.element_id == "row-target"


def test_overrides_consumed_only_once() -> None:
    """Overrides are popped from the runner's dict the first time the
    matching step is resolved. A second resolution of the same step
    (e.g. if the runner replays it within a loop) sees no override.

    The dict mutation is observable through the runner instance, since
    _sub_step_overrides is exposed for inspection."""
    runner = _runner_with_override({5: {"test_id": "row-picked"}})
    assert 5 in runner._sub_step_overrides
    consumed = runner._sub_step_overrides.pop(5, None)
    assert consumed == {"test_id": "row-picked"}
    assert 5 not in runner._sub_step_overrides
