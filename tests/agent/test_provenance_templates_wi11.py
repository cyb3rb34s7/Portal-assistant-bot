"""WI-11: provenance-based templating replaces substring luck.

Acceptance checks (from the plan):
  - ``btn-open-A-9001`` templates to ``btn-open-{asset_id}``, not
    ``btn-open-{search}01``.
  - ``/asset/A-9001`` becomes ``/asset/{asset_id}`` when derived from
    the selected row key.
  - Ambiguous substring candidates are rejected (multiple param values
    matching the same substring).
  - Legacy substring templates remain accepted but are tagged
    ``template_source = "legacy_substring"``.

These tests build skills from raw events (post-WI-02 shape) so we
exercise the full annotator pipeline end to end -- the same code path
a real recording walks through.
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _derive_fingerprint_templates,
    _derive_provenance_templates,
    _derive_url_template,
    _segment_boundary_at,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    NavigationEffect,
    ParamBinding,
    Skill,
    SkillParam,
    SkillStep,
    StepEffect,
    TemplatePart,
    TraceEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helper: token boundary check
# ---------------------------------------------------------------------------


def test_segment_boundary_accepts_separators_and_ends() -> None:
    """A token boundary is non-alphanumeric on either side OR string end."""
    # ``A-9001`` is a clean segment inside ``btn-open-A-9001`` (end of string + dash).
    assert _segment_boundary_at("btn-open-A-9001", 9, 15) is True
    # ``A-90`` inside ``btn-open-A-9001`` has an alphanumeric trailing
    # char (``0``) -- NOT a boundary.
    assert _segment_boundary_at("btn-open-A-9001", 9, 13) is False
    # ``A-9001`` at the start.
    assert _segment_boundary_at("A-9001-action", 0, 6) is True
    # Middle, surrounded by dashes.
    assert _segment_boundary_at("foo/A-9001/bar", 4, 10) is True


# ---------------------------------------------------------------------------
# Row-key provenance: ancestor chain confirms the row identity
# ---------------------------------------------------------------------------


def test_row_key_provenance_templates_btn_open() -> None:
    """The canonical case: click on ``btn-open-A-9001`` whose enclosing
    row has ``data-testid="catalog-row-A-9001"``. The asset_id param's
    recorded value matches the row's testId tail, so the template is
    ``btn-open-{asset_id}`` with source=``row_key``."""
    # The fingerprint at the click target carries ancestor_chain with
    # the row's testId.
    fp_click = ElementFingerprint(
        test_id="btn-open-A-9001",
        tag="button",
        ancestor_chain=[
            {
                "tag": "td",
                "id": None,
                "testId": None,
                "role": None,
                "className": None,
            },
            {
                "tag": "tr",
                "id": None,
                "testId": "catalog-row-A-9001",
                "role": "row",
                "className": None,
            },
        ],
    )
    # A separate step binds the param ``asset_id`` to the recorded value.
    fp_input = ElementFingerprint(test_id="input-asset-id", tag="input")
    skill = _make_skill(
        [
            _make_step(0, action="click", fingerprint=fp_click),
            _make_step(
                1,
                action="change",
                fingerprint=fp_input,
                value="A-9001",
                binding=ParamBinding(name="asset_id", type="string"),
            ),
        ],
        [SkillParam(name="asset_id", type="string", required=True)],
    )
    provenance_count, ambiguous = _derive_provenance_templates(skill)
    assert provenance_count >= 1
    assert fp_click.templates.get("test_id") == "btn-open-{asset_id}"
    assert fp_click.template_sources.get("test_id") == "row_key"
    # Structured template_parts are populated alongside the raw string.
    parts = fp_click.template_parts.get("test_id") or []
    assert any(p.kind == "placeholder" and p.param == "asset_id" for p in parts)
    assert any(
        p.kind == "literal" and p.text == "btn-open-" for p in parts
    )


def test_short_substring_inside_longer_value_rejected() -> None:
    """The audit's BAD case: param ``search=A-90`` must NOT template
    ``btn-open-A-9001`` to ``btn-open-{search}01``. Boundary check
    rejects partial-token matches."""
    fp_click = ElementFingerprint(
        test_id="btn-open-A-9001",
        tag="button",
        # No ancestor with the search query -- only the asset id row.
        ancestor_chain=[
            {"tag": "tr", "id": None, "testId": "catalog-row-A-9001",
             "role": "row", "className": None},
        ],
    )
    # The search param's value 'A-90' is a substring of ``btn-open-A-9001``
    # but NOT on a token boundary (``0`` follows). It must be ignored.
    fp_input = ElementFingerprint(test_id="input-search", tag="input")
    skill = _make_skill(
        [
            _make_step(0, action="click", fingerprint=fp_click),
            _make_step(
                1,
                action="change",
                fingerprint=fp_input,
                value="A-90",
                binding=ParamBinding(name="search", type="string"),
            ),
        ],
        [SkillParam(name="search", type="string", required=True)],
    )
    _derive_provenance_templates(skill)
    # Legacy substring pass should also reject (boundary check applies).
    _derive_fingerprint_templates(skill)
    # No template emitted with {search}.
    tpl = fp_click.templates.get("test_id")
    assert tpl is None or "{search}" not in tpl


def test_ambiguous_two_param_values_match_same_field_rejected() -> None:
    """If two different param values BOTH match the same field on token
    boundaries, the template is ambiguous and the annotator refuses to
    template that field. The literal locator still resolves at L1."""
    fp = ElementFingerprint(
        test_id="row-A-9001-action-B-12345",
        tag="button",
    )
    skill = _make_skill(
        [
            _make_step(0, action="click", fingerprint=fp),
            _make_step(
                1, action="change", value="A-9001",
                binding=ParamBinding(name="asset_id", type="string"),
            ),
            _make_step(
                2, action="change", value="B-12345",
                binding=ParamBinding(name="action_id", type="string"),
            ),
        ],
        [
            SkillParam(name="asset_id", type="string", required=True),
            SkillParam(name="action_id", type="string", required=True),
        ],
    )
    # Provenance pass: no row-key ancestor here, so it relies on
    # _find_unambiguous_match for operator_input provenance. Two
    # candidates match -> bail.
    _derive_provenance_templates(skill)
    assert "test_id" not in fp.template_sources
    # Legacy substring pass ALSO uses the unambiguous matcher now (per
    # WI-11). It must also refuse the field.
    _derive_fingerprint_templates(skill)
    assert "test_id" not in fp.template_sources


# ---------------------------------------------------------------------------
# Legacy substring pass remains accepted, but tagged
# ---------------------------------------------------------------------------


def test_legacy_substring_pass_tags_its_source() -> None:
    """A field templated by the legacy substring pass (no ancestor
    provenance, one unambiguous match) gets ``template_source=
    "legacy_substring"`` so audit code can distinguish weak from strong
    templates."""
    fp = ElementFingerprint(test_id="row-A-9001", tag="tr")
    skill = _make_skill(
        [
            _make_step(0, action="click", fingerprint=fp),
            _make_step(
                1, action="change", value="A-9001",
                binding=ParamBinding(name="content_id", type="string"),
            ),
        ],
        [SkillParam(name="content_id", type="string", required=True)],
    )
    # No ancestor chain on the click target -> provenance pass finds
    # nothing strong, falls back to operator_input which is also legit
    # (the bound value matches exactly once on a boundary).
    _derive_provenance_templates(skill)
    _derive_fingerprint_templates(skill)
    assert fp.templates.get("test_id") == "row-{content_id}"
    src = fp.template_sources.get("test_id")
    # Either operator_input (preferred) or legacy_substring is acceptable
    # depending on whether the provenance pass claimed the field.
    assert src in {"operator_input", "legacy_substring"}


# ---------------------------------------------------------------------------
# URL templating via route_param / row_key provenance
# ---------------------------------------------------------------------------


def test_url_template_route_param() -> None:
    """``/asset/A-9001`` templates to ``/asset/{asset_id}`` when the
    final path segment EXACTLY matches a recorded param value."""
    tmpl, source = _derive_url_template(
        "http://x/asset/A-9001", [("asset_id", "A-9001")]
    )
    assert tmpl == "http://x/asset/{asset_id}"
    assert source == "route_param"


def test_url_template_row_key_source() -> None:
    """When the click target's ancestor chain confirms the row identity,
    a URL segment match upgrades to ``row_key`` source."""
    fp = ElementFingerprint(
        test_id="btn-open-A-9001",
        ancestor_chain=[
            {"tag": "tr", "id": None, "testId": "catalog-row-A-9001",
             "role": "row", "className": None},
        ],
    )
    tmpl, source = _derive_url_template(
        "http://x/asset/A-9001",
        [("asset_id", "A-9001")],
        click_fp=fp,
    )
    assert tmpl == "http://x/asset/{asset_id}"
    assert source == "row_key"


def test_url_template_rejects_substring_luck() -> None:
    """The audit's URL bug: search query ``A-90`` must NOT template
    into ``/asset/A-9001`` as ``/asset/{q}01``. Only exact path-segment
    matches qualify."""
    tmpl, source = _derive_url_template(
        "http://x/asset/A-9001", [("q", "A-90")]
    )
    # Literal URL kept; source=None.
    assert tmpl == "http://x/asset/A-9001"
    assert source is None


def test_url_template_with_query_string_preserved() -> None:
    """Query strings + hash fragments survive templating."""
    tmpl, source = _derive_url_template(
        "http://x/asset/A-9001?tab=meta#preview",
        [("asset_id", "A-9001")],
    )
    assert tmpl == "http://x/asset/{asset_id}?tab=meta#preview"
    assert source == "route_param"


def test_url_template_ambiguous_segment_bails() -> None:
    """Two different params both equal the same path segment ->
    ambiguous; template not emitted."""
    tmpl, source = _derive_url_template(
        "http://x/y/SAME", [("a", "SAME"), ("b", "SAME")]
    )
    # Bail to literal.
    assert tmpl == "http://x/y/SAME"
    assert source is None


# ---------------------------------------------------------------------------
# Roundtrip: provenance fields survive JSON serialization
# ---------------------------------------------------------------------------


def test_template_sources_and_parts_roundtrip() -> None:
    fp = ElementFingerprint(
        test_id="btn-open-A-9001",
        templates={"test_id": "btn-open-{asset_id}"},
        template_sources={"test_id": "row_key"},
        template_parts={
            "test_id": [
                TemplatePart(kind="literal", text="btn-open-"),
                TemplatePart(kind="placeholder", param="asset_id"),
            ]
        },
    )
    step = SkillStep(index=0, action="click", fingerprint=fp)
    skill = Skill(name="t", steps=[step])
    blob = skill.model_dump_json()
    restored = Skill.model_validate_json(blob)
    rfp = restored.steps[0].fingerprint
    assert rfp is not None
    assert rfp.template_sources.get("test_id") == "row_key"
    parts = rfp.template_parts.get("test_id") or []
    assert len(parts) == 2
    assert parts[0].kind == "literal" and parts[0].text == "btn-open-"
    assert parts[1].kind == "placeholder" and parts[1].param == "asset_id"


def test_navigation_effect_url_template_source_roundtrip() -> None:
    step = SkillStep(
        index=0,
        action="click",
        fingerprint=ElementFingerprint(test_id="btn-open-A-9001"),
        effects=StepEffect(
            navigation=NavigationEffect(
                kind="spa_route",
                url="http://x/asset/A-9001",
                url_template="http://x/asset/{asset_id}",
                url_template_source="row_key",
                reload_allowed=False,
            )
        ),
    )
    skill = Skill(name="t", steps=[step])
    blob = skill.model_dump_json()
    restored = Skill.model_validate_json(blob)
    nav = restored.steps[0].effects.navigation  # type: ignore[union-attr]
    assert nav is not None
    assert nav.url_template == "http://x/asset/{asset_id}"
    assert nav.url_template_source == "row_key"


# ---------------------------------------------------------------------------
# End-to-end: build_skill produces row-key templates from a real trace
# shape (click + caused navigate + change with row-key ancestor)
# ---------------------------------------------------------------------------


def test_build_skill_emits_provenance_templates_end_to_end() -> None:
    """Synthetic trace mimicking a Catalog open-button click that
    triggers SPA navigation. The annotator should:
      - emit one click step with effects.navigation (WI-08),
      - template the URL to ``/asset/{asset_id}`` (route_param),
      - template the click target's test_id to
        ``btn-open-{asset_id}`` (row_key).
    """
    click = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id="btn-open-A-9001",
            tag="button",
            ancestor_chain=[
                {"tag": "td", "id": None, "testId": None,
                 "role": None, "className": None},
                {"tag": "tr", "id": None, "testId": "catalog-row-A-9001",
                 "role": "row", "className": None},
            ],
        ),
        page_url="http://localhost:5188/catalog",
        event_id="c1",
        interaction_id="c1",
        caused_by=None,
        sequence=1,
        source="user_click",
    )
    nav = TraceEvent(
        ts=datetime.utcnow(),
        kind="navigate",
        url="http://localhost:5188/asset/A-9001",
        page_url="http://localhost:5188/asset/A-9001",
        event_id="n1",
        caused_by="c1",
        interaction_id="c1",
        sequence=2,
        source="history.pushState",
        raw_event_kind="history.pushState",
    )
    # A subsequent input binds the value ``A-9001`` to the param. The
    # ``name`` attribute drives the auto-named binding so the param is
    # ``asset_id`` and the template ends up with ``{asset_id}``.
    fill = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id="input-asset-id", tag="input", name="asset_id",
        ),
        value="A-9001",
        page_url="http://localhost:5188/asset/A-9001",
        event_id="i1",
        interaction_id="i1",
        caused_by=None,
        sequence=3,
        source="user_input",
    )
    skill = build_skill(
        skill_name="open_asset",
        events=[click, nav, fill],
        auto=True,
    )
    # One click + one fill (navigate folded into click).
    assert len(skill.steps) == 2
    click_step = skill.steps[0]
    assert click_step.action == "click"
    assert click_step.fingerprint is not None
    # Test_id templated via row_key provenance.
    assert click_step.fingerprint.templates.get("test_id") == "btn-open-{asset_id}"
    assert click_step.fingerprint.template_sources.get("test_id") == "row_key"
    # URL templated via route_param (or row_key when ancestor matches).
    nav_eff = click_step.effects and click_step.effects.navigation
    assert nav_eff is not None
    assert nav_eff.url_template == "http://localhost:5188/asset/{asset_id}"
    assert nav_eff.url_template_source in {"route_param", "row_key"}
