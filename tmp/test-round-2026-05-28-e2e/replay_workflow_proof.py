"""Prove the destructive _robust_click + transition path end-to-end on a
fresh draft (D-4002): the CLI/runner replay failed with target_disabled
(WI-43) because the asset GET hadn't settled and the navigate step carries
no readiness signal. Here we navigate, WAIT for the Submit button to enable
(real asset-load signal), then run the runner with auto-approve so the
destructive click fires. Verify status -> in_review.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "tmp/test-round-2026-05-28-e2e")
from harness import CDP, BASE, connect_to_chrome, console

from pilot.skill_runner import SkillRunner
from pilot.skill_models import Skill

ASSET = "D-4002"
skill = Skill.model_validate(json.loads(Path("skills/workflow_submit.json").read_text(encoding="utf-8")))
# Drop the navigate step's reload race: navigate ourselves + wait for the
# button to enable, then let the runner do ONLY the destructive click.
skill.steps = [s for s in skill.steps if s.action == "click"]

session = connect_to_chrome(CDP, target_url_substring=BASE)
try:
    page = session.page
    page.goto(f"{BASE}/asset/{ASSET}", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=asset-edit-form]", timeout=10000)
    # Real readiness signal: Submit button enabled (asset GET settled).
    page.wait_for_function(
        "() => {const b=document.querySelector('[data-testid=btn-submit-review]');"
        "return b && !b.disabled;}",
        timeout=10000,
    )
    before = page.eval_on_selector("[data-testid=asset-status]", "e=>e.textContent.trim()")
    console.print(f"D-4002 status before: {before}")
    runner = SkillRunner(
        session=session, skill=skill, params={},
        sessions_dir=Path("sessions"), base_url=f"{BASE}/asset/{ASSET}",
        approve_fn=lambda step: True,
    )
    results = runner.run()
    for step, res, level in results:
        console.print(f"step {step.index} {step.action} -> success={res.success} :: {res.action_taken[:80]}")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=asset-status]').textContent.trim()==='in_review'",
        timeout=12000,
    )
    after = page.eval_on_selector("[data-testid=asset-status]", "e=>e.textContent.trim()")
    console.print(f"D-4002 status after: {after}")
finally:
    session.close()
