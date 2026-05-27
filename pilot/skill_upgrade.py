"""WI-50: legacy skill upgrader.

Takes a v1 skill JSON (the shape captured before the structural fix
sprint -- raw ``action`` + ``fingerprint`` + ``url`` / ``value`` /
``file_path`` per step, no ``schema_version`` field, no ``effects``, no
``replay_policy``, no ``provenance``) and produces a v2 skill JSON that
the current ``Skill`` model + ``SkillRunner`` accept.

Design constraints:

1. **Pure dict-in, dict-out.** No pydantic dependency in the upgrade
   path itself -- we operate on the JSON shape, then a separate caller
   can validate by constructing ``Skill.model_validate(upgraded_dict)``.
   This keeps the upgrader runnable in tools that don't ship the full
   pydantic model surface (CI lint, batch migration scripts, etc.).

2. **Idempotent.** Upgrading a v2 skill returns the same v2 skill.
   ``upgrade_skill_to_v2(upgrade_skill_to_v2(s)) == upgrade_skill_to_v2(s)``.
   The caller can run it unconditionally on every load without
   accumulating drift.

3. **Preserve legacy behavior.** Pre-sprint skills predate the fail-fast
   default (WI-07). If we slammed those into ``on_failure="abort"`` they
   would start aborting mid-flow on every flaky step, which is a regression
   from the operator's perspective even though it's "more correct" by
   the new policy. Legacy upgraded steps get
   ``replay_policy.on_failure="continue"`` so the runner behaves exactly
   as it did pre-WI-07. New annotations (which set the field explicitly)
   keep their declared policy.

4. **No fabrication.** Fields we can't derive from the legacy shape are
   left at their schema default (None / [] / safe-disabled). The
   upgrader doesn't invent ``effects``, ``expected_signals``, or
   ``provenance`` -- those need raw trace evidence the v1 JSON doesn't
   carry.

Stamps two markers on the result:

  - ``schema_version = 2``  -- gates schema-version-aware code paths.
  - ``upgraded_from`` (Skill model attr added in this WI) -- audit
    breadcrumb so we can tell "was this written by a v1 recorder and
    upgraded?" vs "was this written natively at v2?".
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


CURRENT_SCHEMA_VERSION = 2
"""Current Skill.schema_version. Bumped by this WI from 1 -> 2 so the
upgrader has a meaningful target. ``Skill.schema_version`` field default
remains 1 so legacy v1 JSON without the field still loads as-is for
introspection; the upgrader is the canonical path to v2."""


def upgrade_skill_to_v2(skill_json: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a legacy v1 skill dict to v2.

    Idempotent. Pure (does not mutate the input).

    Returns a new dict. Use ``Skill.model_validate(result)`` to validate.
    """
    if not isinstance(skill_json, dict):
        raise TypeError(
            f"upgrade_skill_to_v2 expects dict, got {type(skill_json).__name__}"
        )
    out = deepcopy(skill_json)

    existing_version = out.get("schema_version")
    if existing_version is None:
        existing_version = 1  # pre-sprint skills omit the field entirely
    if not isinstance(existing_version, int):
        # Garbage-in safety: anything non-int gets treated as v1.
        existing_version = 1

    is_legacy = existing_version < CURRENT_SCHEMA_VERSION

    # Stamp the version + breadcrumb. We do this BEFORE step migration
    # so the marker is present even if step migration is a no-op (the
    # input might already have effects/replay_policy populated by a
    # half-upgraded recorder).
    out["schema_version"] = CURRENT_SCHEMA_VERSION
    if is_legacy:
        # Only record the upgrade-from marker once. Re-upgrading a v2
        # that previously came from v1 should not overwrite the original
        # source version.
        out.setdefault("upgraded_from", existing_version)

    # Step migration: walk every step and fill in the v2 fields if missing.
    raw_steps = out.get("steps")
    if isinstance(raw_steps, list):
        out["steps"] = [
            _upgrade_step(step, is_legacy=is_legacy) for step in raw_steps
        ]

    # Params: legacy v1 params already match the v2 shape (name/type/
    # description/example/required/depends_on/select_from_current_options).
    # Newer fields (constraints, codec, provenance) are optional with
    # defaults, so legacy params load without modification.
    raw_params = out.get("params")
    if isinstance(raw_params, list):
        out["params"] = [_upgrade_param(p) for p in raw_params]

    return out


def _upgrade_step(step: Any, *, is_legacy: bool) -> dict[str, Any]:
    """Apply v2-shape fixes to one step dict. Idempotent."""
    if not isinstance(step, dict):
        # Bad data: leave it for pydantic to reject downstream rather
        # than silently dropping it here.
        return step

    s = dict(step)  # shallow copy is fine; we don't mutate nested dicts.

    # replay_policy. v1 had no field at all. v2 default is the
    # fail-fast ReplayPolicy() = on_failure="abort". For legacy upgrades
    # we want to PRESERVE the pre-WI-07 behavior of continuing past
    # transient failures -- the operator built these flows expecting
    # that. New annotations written natively at v2 already carry their
    # intended policy.
    if "replay_policy" not in s or s["replay_policy"] is None:
        if is_legacy:
            s["replay_policy"] = {
                "on_failure": "continue",
                "reload_allowed": False,
                "optional": False,
            }
        else:
            # Non-legacy without a policy = explicit caller intent to
            # use defaults. Leave it absent so pydantic fills the
            # current default (abort).
            pass

    # provenance: v1 has none. Leave as None (Skill loads it as Optional[StepProvenance]).
    s.setdefault("provenance", None)

    # effects: v1 has none. Leave as None.
    s.setdefault("effects", None)

    # expected_signals + assert_after: v1 already supports these in JSON
    # (they were the "additive" path in the prior sprint). Default to
    # None / [] if absent so re-serializing produces a stable shape.
    s.setdefault("expected_signals", None)
    if s.get("assert_after") is None:
        s["assert_after"] = []

    # ambiguity_policy / repair_policy / disambiguation_hint / set_selection
    # / spec fields: all Optional in v2. Leave None when absent. We do
    # NOT add empty dicts -- pydantic distinguishes "field absent" from
    # "field={}".
    for field in (
        "ambiguity_policy",
        "repair_policy",
        "disambiguation_hint",
        "set_selection",
        "fill_submit",
        "select_autocomplete",
        "select_option",
        "dependency_chain",
        "date_select",
        "slider_set",
        "shortcut",
        "rich_text",
        "file_spec",
        "drag_drop",
        "download_spec",
        "canvas_gesture",
        "toggle_state",
        "scroll_until",
        "page_context",
        "auth_precondition",
        "value_transition",
        "click_gesture",
        "effect_signature",
    ):
        s.setdefault(field, None)

    # strict_locale: v1 had no concept. Default False (warn, don't fail).
    s.setdefault("strict_locale", False)

    return s


def _upgrade_param(param: Any) -> dict[str, Any]:
    """Apply v2-shape fixes to one param dict. Idempotent.

    Note: ``codec`` defaults to ``"raw"`` on SkillParam (not Optional),
    so legacy params get the pass-through codec rather than None.
    ``constraints`` and ``provenance`` are Optional and default to None.
    """
    if not isinstance(param, dict):
        return param
    p = dict(param)
    p.setdefault("constraints", None)
    if "codec" not in p or p["codec"] is None:
        p["codec"] = "raw"
    p.setdefault("provenance", None)
    return p


# ---------------------------------------------------------------------------
# File-level helper for the CLI
# ---------------------------------------------------------------------------


def upgrade_skill_file(path: Path) -> dict[str, Any]:
    """Read a skill JSON from disk, upgrade it, write it back.

    Returns the upgraded dict. Raises if the file can't be read or
    parsed. Writes back with 2-space indent to match the existing
    skills/*.json formatting convention.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    upgraded = upgrade_skill_to_v2(raw)
    path.write_text(
        json.dumps(upgraded, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return upgraded
