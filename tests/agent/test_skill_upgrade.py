"""WI-50: skill upgrader unit tests.

Pins the contract:

  - Legacy v1 skill JSON (no ``schema_version``, no ``replay_policy``,
    no ``effects``, no ``provenance``) upgrades cleanly and the result
    validates against the current Skill model.
  - Upgrading is idempotent: ``upgrade(upgrade(x)) == upgrade(x)``.
  - Legacy steps get ``replay_policy.on_failure="continue"`` so the
    pre-WI-07 behavior (don't fail-fast on flaky steps) survives the
    migration.
  - The upgraded Skill loads + the runner accepts it without errors
    (we exercise this via the Skill model validation -- the runner
    consumes the same model).
  - The ``Skill.upgraded_from`` audit breadcrumb is set on legacy
    migrations and preserved on re-upgrade.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

from pilot.skill_models import Skill
from pilot.skill_upgrade import (
    CURRENT_SCHEMA_VERSION,
    upgrade_skill_file,
    upgrade_skill_to_v2,
)


def _legacy_v1_skill_dict() -> dict:
    """Minimal v1-shape skill dict matching the pre-sprint format.

    No schema_version, no replay_policy, no effects, no provenance --
    just the legacy ``action`` + ``fingerprint`` + payload-per-step
    shape. Mirrors what's in ``skills/change_title.json`` after the
    earlier additive sprint but before WI-01.
    """
    return {
        "name": "legacy_t",
        "description": "",
        "version": 1,
        "params": [
            {
                "name": "input_search",
                "type": "string",
                "description": "Search input",
                "example": "A-9001",
                "required": True,
                "depends_on": None,
                "select_from_current_options": False,
            },
        ],
        "steps": [
            {
                "index": 0,
                "action": "navigate",
                "url": "http://localhost:5188/catalog",
            },
            {
                "index": 1,
                "action": "change",
                "fingerprint": {"test_id": "input-catalog-search"},
                "value": "A-9001",
            },
            {
                "index": 2,
                "action": "click",
                "fingerprint": {"test_id": "btn-search"},
            },
        ],
    }


def test_legacy_skill_loads_and_upgrades() -> None:
    """A v1 skill dict (no schema_version) round-trips through the
    upgrader and validates against the current Skill model."""
    legacy = _legacy_v1_skill_dict()
    upgraded = upgrade_skill_to_v2(legacy)

    assert upgraded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert upgraded["upgraded_from"] == 1

    # Skill model validates the result.
    s = Skill.model_validate(upgraded)
    assert s.schema_version == CURRENT_SCHEMA_VERSION
    assert s.upgraded_from == 1
    assert len(s.steps) == 3


def test_upgrader_is_idempotent() -> None:
    """Running the upgrader twice produces the same dict the second
    time as the first."""
    legacy = _legacy_v1_skill_dict()
    once = upgrade_skill_to_v2(legacy)
    twice = upgrade_skill_to_v2(once)
    assert once == twice, (
        "upgrader is not idempotent -- second call produced "
        "different output"
    )


def test_upgrader_preserves_legacy_continue_on_failure() -> None:
    """Legacy v1 skills predate the fail-fast default (WI-07). The
    upgrader stamps ``on_failure='continue'`` so existing flows don't
    start aborting mid-stream on transient failures."""
    legacy = _legacy_v1_skill_dict()
    upgraded = upgrade_skill_to_v2(legacy)
    for step in upgraded["steps"]:
        rp = step.get("replay_policy")
        assert rp is not None, "legacy step missing replay_policy"
        assert rp["on_failure"] == "continue", (
            f"legacy step {step['index']} got "
            f"on_failure={rp['on_failure']!r}, expected 'continue'"
        )


def test_upgrader_does_not_overwrite_existing_replay_policy() -> None:
    """If a legacy step already declares a replay_policy (e.g. an
    operator hand-edited it), the upgrader leaves it alone."""
    legacy = _legacy_v1_skill_dict()
    legacy["steps"][1]["replay_policy"] = {
        "on_failure": "abort",
        "reload_allowed": False,
        "optional": False,
    }
    upgraded = upgrade_skill_to_v2(legacy)
    assert upgraded["steps"][1]["replay_policy"]["on_failure"] == "abort"


def test_upgrader_v2_input_is_noop() -> None:
    """A v2 skill that already has schema_version=2 is unchanged
    structurally and is NOT marked upgraded_from."""
    legacy = _legacy_v1_skill_dict()
    legacy["schema_version"] = CURRENT_SCHEMA_VERSION
    # Stamp replay_policy as the runner would for natively-v2 steps.
    for step in legacy["steps"]:
        step["replay_policy"] = {
            "on_failure": "abort",
            "reload_allowed": False,
            "optional": False,
        }

    upgraded = upgrade_skill_to_v2(legacy)
    assert upgraded["schema_version"] == CURRENT_SCHEMA_VERSION
    assert upgraded.get("upgraded_from") is None, (
        "non-legacy v2 input must not get the upgraded_from breadcrumb"
    )
    for step in upgraded["steps"]:
        assert step["replay_policy"]["on_failure"] == "abort"


def test_upgrader_does_not_mutate_input() -> None:
    """``upgrade_skill_to_v2`` is pure -- the caller's dict is not
    modified in place."""
    legacy = _legacy_v1_skill_dict()
    before = copy.deepcopy(legacy)
    upgrade_skill_to_v2(legacy)
    assert legacy == before, "upgrader mutated its input"


def test_upgraded_skill_constructs_runner_compatible_skill() -> None:
    """Upgraded skill builds without errors and carries the v2 fields
    the runner relies on (schema_version, per-step replay_policy)."""
    legacy = _legacy_v1_skill_dict()
    upgraded = upgrade_skill_to_v2(legacy)
    s = Skill.model_validate(upgraded)

    # Schema-version-aware code paths gate on this exact value.
    assert s.schema_version == 2

    # Every step has a ReplayPolicy materialized (the runner walks
    # step.replay_policy.on_failure unconditionally).
    for step in s.steps:
        assert step.replay_policy is not None
        # Legacy continuation policy preserved.
        assert step.replay_policy.on_failure == "continue"


def test_real_legacy_skill_in_repo_upgrades() -> None:
    """End-to-end smoke: the actual ``skills/change_title.json`` checked
    into the repo is a v1 recording. It must upgrade cleanly."""
    path = Path("skills/change_title.json")
    if not path.exists():
        # Repo without the sample skill -- skip rather than fail.
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    upgraded = upgrade_skill_to_v2(raw)
    assert upgraded["schema_version"] == CURRENT_SCHEMA_VERSION
    # change_title.json predates WI-01 -- it must be flagged as legacy.
    assert upgraded["upgraded_from"] == 1
    # And it must validate.
    s = Skill.model_validate(upgraded)
    assert s.name == "change_title"


def test_upgrade_skill_file_round_trip(tmp_path: Path) -> None:
    """``upgrade_skill_file`` reads, upgrades, and writes back. Running
    twice on the same file leaves it unchanged."""
    p = tmp_path / "demo.json"
    p.write_text(
        json.dumps(_legacy_v1_skill_dict()), encoding="utf-8"
    )

    upgraded_1 = upgrade_skill_file(p)
    first_disk = p.read_text(encoding="utf-8")
    upgraded_2 = upgrade_skill_file(p)
    second_disk = p.read_text(encoding="utf-8")

    assert upgraded_1["schema_version"] == CURRENT_SCHEMA_VERSION
    assert upgraded_2 == upgraded_1
    assert first_disk == second_disk, (
        "upgrade_skill_file rewrote the file with different content "
        "on the second pass -- not idempotent on disk"
    )
