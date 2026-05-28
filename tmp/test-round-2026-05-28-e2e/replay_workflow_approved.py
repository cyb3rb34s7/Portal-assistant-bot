"""Replay the destructive workflow_submit skill on a FRESH draft (D-4001)
through the REAL SkillRunner, auto-approving the human gate (the gate
itself was already proven to BLOCK without approval in the CLI run).
Then verify status -> in_review via the backend.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "tmp/test-round-2026-05-28-e2e")
from harness import CDP, BASE, connect_to_chrome, console

from pilot.skill_runner import SkillRunner
from pilot.skill_models import Skill

ASSET = "D-4001"
skill = Skill.model_validate(json.loads(Path("skills/workflow_submit.json").read_text(encoding="utf-8")))
session = connect_to_chrome(CDP, target_url_substring=BASE)
try:
    runner = SkillRunner(
        session=session,
        skill=skill,
        params={},
        sessions_dir=Path("sessions"),
        base_url=f"{BASE}/asset/{ASSET}",
        approve_fn=lambda step: True,  # auto-approve the destructive gate
    )
    results = runner.run()
    for step, res, level in results:
        console.print(f"step {step.index} {step.action} {step.semantic_label} -> success={res.success} level={level} :: {res.action_taken}")
finally:
    session.close()
