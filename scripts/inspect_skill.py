"""Pretty-print a skill JSON + its v2 sidecar (if present).

Usage:
    py scripts/inspect_skill.py skills/my_curation.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: py scripts/inspect_skill.py <path-to-skill.json>")
        return 2
    skill_path = Path(sys.argv[1])
    if not skill_path.exists():
        print(f"not found: {skill_path}")
        return 2

    skill = json.loads(skill_path.read_text(encoding="utf-8"))
    print(f"=== {skill_path.name} ===")
    print(f"name:        {skill.get('name')}")
    print(f"description: {(skill.get('description') or '')[:80]}")
    print(f"steps:       {len(skill.get('steps', []))}")
    print(f"v1 params ({len(skill.get('params', []))}):")
    for p in skill.get("params", []):
        print(f"  {p.get('name'):35s}  {p.get('type', 'string'):10s}  example={p.get('example')!r}")

    sidecar = skill_path.with_suffix(".v2.json")
    if not sidecar.exists():
        print(f"\n(no v2 sidecar at {sidecar.name})")
        return 0

    side = json.loads(sidecar.read_text(encoding="utf-8"))
    print(f"\n=== {sidecar.name} (LLM-enriched) ===")
    print(f"description: {(side.get('description') or '')[:120]}")
    print(f"v2 parameters ({len(side.get('parameters', []))}):")
    for p in side.get("parameters", []):
        print(
            f"  {p.get('name'):35s}  {p.get('type'):10s}  "
            f"hint={(p.get('source_hint') or '')[:50]!r}"
        )
    aliases = side.get("param_alias_map", {})
    if aliases:
        print(f"\nalias map (semantic -> recorded v1 name):")
        for k, v in aliases.items():
            print(f"  {k:35s} -> {v}")
    if side.get("destructive_actions"):
        print(f"\ndestructive actions ({len(side['destructive_actions'])}):")
        for d in side["destructive_actions"]:
            print(f"  step {d.get('step'):3d}  kind={d.get('kind')}  reversible={d.get('reversible')}")
    if side.get("success_assertions"):
        print(f"\nsuccess assertions:")
        for a in side["success_assertions"]:
            print(f"  {a.get('type')}: {a.get('text') or a.get('pattern') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
