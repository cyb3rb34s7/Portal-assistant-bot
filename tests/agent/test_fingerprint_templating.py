"""Unit tests for the annotate-time fingerprint templating + the
runner-time materialization of templates.

The behaviour these tests pin down: when a recorded fingerprint has a
field (testid, css_path, etc.) that contains the value of one of the
recorded params, that field gets templated as ``"...{param_name}..."``
at annotate time. At replay, ``_materialize_fingerprint`` substitutes
the current parameter value before locator resolution — so a recording
made with ``content_id=A-9001`` correctly drives a replay with
``content_id=B-12345``.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import _derive_fingerprint_templates
from pilot.skill_models import (
    ElementFingerprint,
    ParamBinding,
    Skill,
    SkillParam,
    SkillStep,
)


def _make_step(
    idx: int,
    *,
    action: str = "click",
    fingerprint: ElementFingerprint | None = None,
    value: str | None = None,
    binding: ParamBinding | None = None,
) -> SkillStep:
    return SkillStep(
        index=idx,
        action=action,
        fingerprint=fingerprint,
        value=value,
        param_binding=binding,
    )


def _make_skill(steps: list[SkillStep], params: list[SkillParam]) -> Skill:
    return Skill(
        name="t",
        params=params,
        steps=steps,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


def test_templates_dom_id_encoding_param_value() -> None:
    """The classic enterprise-portal pattern: the click-row step's
    testid contains the content_id (``row-A-9001``) and a separate
    fill step records ``A-9001`` as the param value. Templating
    should detect this and produce ``row-{content_id}``."""
    fp_row = ElementFingerprint(
        test_id="row-A-9001",
        role="row",
        tag="tr",
    )
    fp_input = ElementFingerprint(
        test_id="input-content-id",
        role="textbox",
        tag="input",
    )
    skill = _make_skill(
        [
            _make_step(0, action="click", fingerprint=fp_row),
            _make_step(
                1,
                action="change",
                fingerprint=fp_input,
                value="A-9001",
                binding=ParamBinding(name="content_id", type="string"),
            ),
        ],
        [SkillParam(name="content_id", type="string", required=True)],
    )

    templated_count = _derive_fingerprint_templates(skill)

    assert templated_count >= 1
    assert skill.steps[0].fingerprint.templates.get("test_id") == "row-{content_id}"


def test_template_substitution_at_replay() -> None:
    """The runner's materialize step substitutes the current
    parameter value into the template field. Tested in isolation by
    constructing a fingerprint with a template and applying the
    runner's materialization logic via a small adapter."""
    fp = ElementFingerprint(
        test_id="row-A-9001",
        templates={"test_id": "row-{content_id}"},
    )

    # Reuse the runner's logic without spinning up a SkillRunner;
    # the materialize is a simple function of (fp, params).
    from pilot.skill_runner import SkillRunner

    class _StubRunner:
        params = {"content_id": "B-12345"}
        # Reuse the bound method without instantiating the full runner.
        _materialize_fingerprint = SkillRunner._materialize_fingerprint  # type: ignore[attr-defined]

    materialized = _StubRunner()._materialize_fingerprint(fp)
    assert materialized.test_id == "row-B-12345"
    # Original is untouched
    assert fp.test_id == "row-A-9001"


def test_template_substitution_skips_when_param_missing() -> None:
    """If the template references a param that wasn't supplied for this
    run, the original literal is kept (and downstream L2/L3 will deal
    with it). We don't crash."""
    fp = ElementFingerprint(
        test_id="row-A-9001",
        templates={"test_id": "row-{content_id}"},
    )

    from pilot.skill_runner import SkillRunner

    class _StubRunner:
        params: dict = {}
        _materialize_fingerprint = SkillRunner._materialize_fingerprint  # type: ignore[attr-defined]

    materialized = _StubRunner()._materialize_fingerprint(fp)
    # Param missing -> original test_id preserved
    assert materialized.test_id == "row-A-9001"


def test_short_param_values_skipped() -> None:
    """Param values shorter than 3 chars are skipped to avoid
    accidentally templating common substrings (e.g. content_id="A"
    matching every "A" in the page DOM)."""
    fp = ElementFingerprint(test_id="header-A-row")
    skill = _make_skill(
        [
            _make_step(0, action="click", fingerprint=fp),
            _make_step(
                1,
                action="change",
                value="A",  # too short
                binding=ParamBinding(name="single_letter", type="string"),
            ),
        ],
        [SkillParam(name="single_letter", type="string", required=True)],
    )
    _derive_fingerprint_templates(skill)
    # Should not have templated test_id from the 'A' substring
    assert "test_id" not in fp.templates
