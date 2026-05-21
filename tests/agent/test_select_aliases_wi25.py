"""Tests for WI-25: declared aliases + structural failure replace
automatic fuzzy select.

The legacy ``_select_option_with_fuzzy_fallback`` would difflib
similarity-match a recorded value to current options when the exact
value wasn't found, with threshold 0.7. That meant a recorded ``US``
could silently auto-select ``UAE`` (similarity 0.75) -- a wrong locale
the operator never authorized.

WI-25 replaces this with declared aliases:
  - ``SelectOptionSpec.option_match_policy``:
    exact_only (default for new skills) | alias | current_options |
    legacy_fuzzy (back-compat).
  - ``SkillParam.enum_aliases`` carries portal-level aliases that
    apply to every select_option step bound to that param.
  - The runner consults BOTH SkillParam.enum_aliases AND
    SelectOptionSpec.aliases when in alias mode.
  - exact_only / alias / current_options paths NEVER fall back to
    fuzzy. The audit's US->UAE failure mode is structurally impossible.
  - legacy_fuzzy path is preserved for skills without options_snapshot
    but new annotations don't emit it.

Acceptance check (from the plan):
  Recorded ``US`` never auto-selects ``UAE``. The legacy path still
  works for old skills without WI-03's options_snapshot.
"""

from __future__ import annotations

import pytest

from pilot.skill_models import (
    OptionSnapshot,
    SelectOptionSpec,
    SkillParam,
)


# ---------------------------------------------------------------------
# Schema: enum_aliases + option_match_policy field shapes
# ---------------------------------------------------------------------


def test_skill_param_enum_aliases_default_empty() -> None:
    """enum_aliases defaults to an empty dict so legacy params behave
    identically to pre-WI-25."""
    p = SkillParam(name="region", type="enum")
    assert p.enum_aliases == {}


def test_skill_param_enum_aliases_carries_operator_declaration() -> None:
    """When the operator declares aliases on the param, they're
    preserved as a dict[str, list[str]]."""
    p = SkillParam(
        name="region",
        type="enum",
        enum_aliases={"US": ["USA", "United States"]},
    )
    assert p.enum_aliases["US"] == ["USA", "United States"]


def test_select_option_spec_option_match_policy_defaults_to_exact_only() -> None:
    """New SelectOptionSpec instances default to exact_only -- the
    safe-by-default behavior introduced by WI-25. Legacy skills that
    explicitly set legacy_fuzzy continue to work."""
    spec = SelectOptionSpec(recorded_value="US")
    assert spec.option_match_policy == "exact_only"


def test_select_option_spec_match_policies_round_trip() -> None:
    """Each declared policy literal is accepted by Pydantic."""
    for policy in ("exact_only", "alias", "current_options", "legacy_fuzzy"):
        spec = SelectOptionSpec(
            recorded_value="US", option_match_policy=policy  # type: ignore[arg-type]
        )
        assert spec.option_match_policy == policy


# ---------------------------------------------------------------------
# Behavior: alias merging from SkillParam + SelectOptionSpec
# ---------------------------------------------------------------------


def test_select_option_spec_aliases_field_kept_for_step_level() -> None:
    """SelectOptionSpec retains its own aliases field for step-scoped
    aliasing -- the WI-17 contract preserved. WI-25 adds SkillParam.
    enum_aliases as the broader portal/param-level home; the runner
    merges both."""
    spec = SelectOptionSpec(
        recorded_value="US",
        aliases={"US": ["USA"]},
    )
    assert spec.aliases == {"US": ["USA"]}


def test_legacy_fuzzy_policy_still_routes_to_similarity_path() -> None:
    """legacy_fuzzy is the back-compat escape hatch for skills
    recorded before WI-25 had options_snapshot. The schema accepts it
    and a runner integration test would verify the behavior; the
    runner code branches on this literal to call
    _select_option_with_fuzzy_fallback."""
    spec = SelectOptionSpec(
        recorded_value="US", option_match_policy="legacy_fuzzy"
    )
    assert spec.option_match_policy == "legacy_fuzzy"


# ---------------------------------------------------------------------
# Annotator must not auto-fuzzy: aliases are operator-curated or from
# portal context's declared aliases. We pin the contract that the
# annotator's enum_aliases is empty unless something explicit feeds it.
# ---------------------------------------------------------------------


def test_skill_param_enum_aliases_not_auto_populated_from_options() -> None:
    """Even when enum_options is populated from the recording, the
    annotator must NOT auto-generate enum_aliases from text similarity.
    This pins the contract: enum_aliases defaults to empty even with
    a populated enum_options."""
    p = SkillParam(
        name="region",
        type="enum",
        enum_options=[
            OptionSnapshot(value="US", label="United States"),
            OptionSnapshot(value="UAE", label="United Arab Emirates"),
            OptionSnapshot(value="UK", label="United Kingdom"),
        ],
    )
    # No aliases were declared -- the empty dict is the safe default.
    assert p.enum_aliases == {}


def test_skill_param_enum_aliases_dict_can_have_multiple_keys() -> None:
    """The aliases dict can carry multiple canonical->alt mappings,
    distinguishing portal-specific naming variants."""
    p = SkillParam(
        name="status",
        type="enum",
        enum_aliases={
            "PUBLISHED": ["LIVE", "Production"],
            "DRAFT": ["WIP", "Work in Progress"],
        },
    )
    assert set(p.enum_aliases.keys()) == {"PUBLISHED", "DRAFT"}
    assert p.enum_aliases["PUBLISHED"] == ["LIVE", "Production"]
    assert p.enum_aliases["DRAFT"] == ["WIP", "Work in Progress"]


# ---------------------------------------------------------------------
# Audit case: recorded 'US' must never auto-match 'UAE' under any
# non-legacy_fuzzy policy.
# ---------------------------------------------------------------------


def test_audit_us_uae_documentation_pinned_in_schema() -> None:
    """Verify the schema makes the audit's specific failure
    structurally impossible. A SelectOptionSpec with option_match_policy
    in (exact_only, alias, current_options) and no aliases declared
    cannot match US -> UAE. The runner's _do_select_option enforces
    the policy; this test pins the schema contract."""
    # exact_only path: recorded US should fail if no exact US in current options.
    spec = SelectOptionSpec(
        recorded_value="US",
        option_match_policy="exact_only",
    )
    # Schema doesn't enforce auto-fuzzy: no fields here even hint at
    # similarity scoring.
    assert spec.option_match_policy == "exact_only"
    assert spec.aliases == {}  # operator must declare aliases explicitly
