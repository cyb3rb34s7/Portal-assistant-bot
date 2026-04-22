"""Resilience test: prove the 4-level fallback works under drift.

Takes an already-recorded skill (skills/curate_one_item.json),
corrupts the test_id on one of the step fingerprints so L1 fails,
and replays the skill. Expects the replay to still succeed via L2
semantic locators (role + accessible name) or L3 best-match.

Usage:
    .venv/Scripts/python.exe scripts/resilience_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "sessions"
SKILLS_DIR = ROOT / "skills"
PORTAL_DIR = ROOT / "sample_portal"
CHROME_PROFILE = Path(os.environ["USERPROFILE"]) / ".curationpilot-e2e-profile"
CHROME_BIN = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PORTAL_URL = "http://localhost:5173"
CDP_URL = "http://localhost:9222"


def info(msg: str) -> None:
    print(f"[resil] {msg}", flush=True)


def wait_for(url: str, timeout: float = 30.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_portal() -> subprocess.Popen:
    proc = subprocess.Popen(
        ["npm.cmd", "run", "dev"],
        cwd=str(PORTAL_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    if not wait_for(PORTAL_URL, 45):
        proc.kill()
        raise RuntimeError("portal did not come up")
    return proc


def start_chrome() -> subprocess.Popen:
    if CHROME_PROFILE.exists():
        shutil.rmtree(CHROME_PROFILE, ignore_errors=True)
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            str(CHROME_BIN),
            "--remote-debugging-port=9222",
            f"--user-data-dir={CHROME_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            PORTAL_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    if not wait_for(CDP_URL + "/json/version", 30):
        proc.kill()
        raise RuntimeError("CDP did not come up")
    return proc


def stop_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        proc.kill()


def main() -> int:
    skill_path = SKILLS_DIR / "curate_one_item.json"
    if not skill_path.exists():
        info(f"skill not found — run scripts/e2e_test.py first")
        return 2

    # Corrupt the test_id of the 'click_btn_save_layout' and
    # 'click_curation_tab_schedule' steps so L1 must fail.
    drifted_path = SKILLS_DIR / "curate_one_item_drifted.json"
    data = json.loads(skill_path.read_text(encoding="utf-8"))

    corrupted_steps = 0
    for s in data["steps"]:
        if not s.get("fingerprint"):
            continue
        fp = s["fingerprint"]
        testid = fp.get("test_id") or ""
        if testid in (
            "btn-save-layout",
            "curation-tab-schedule",
            "btn-save-schedule",
        ):
            fp["test_id"] = testid + "_RENAMED_BY_DRIFT"
            fp["element_id"] = (fp.get("element_id") or "") + "_DRIFT"
            fp["css_path"] = None         # also kill the direct CSS path
            fp["xpath"] = None
            corrupted_steps += 1
    info(f"corrupted L1 locators on {corrupted_steps} steps")
    drifted_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    info(f"wrote drifted skill -> {drifted_path}")

    vite = start_portal()
    chrome = start_chrome()

    try:
        from pilot.browser import connect_to_chrome
        from pilot.skill_runner import SkillRunner
        from pilot.skill_models import Skill

        info("connecting via CDP ...")
        session = connect_to_chrome(CDP_URL, target_url_substring=PORTAL_URL)
        try:
            skill = Skill.model_validate_json(drifted_path.read_text(encoding="utf-8"))
            params = {p.name: (p.example or "") for p in skill.params}
            for p in skill.params:
                if "content" in p.name.lower():
                    params[p.name] = "A-9900"
            info(f"replay params: {params}")

            # reset state
            session.page.goto(PORTAL_URL + "/dashboard")
            session.page.wait_for_timeout(300)

            runner = SkillRunner(
                session=session,
                skill=skill,
                params=params,
                sessions_dir=SESSIONS_DIR,
                base_url=PORTAL_URL,
                approve_fn=lambda step: True,
                takeover_fn=lambda step: False,
            )
            results = runner.run()

            fails = [(s, r, lvl) for (s, r, lvl) in results if not r.success]
            info(f"results: {len(results)} steps, {len(fails)} failures")

            # Count fallback levels used
            from collections import Counter
            level_counts = Counter(lvl for (_, _, lvl) in results)
            info(f"levels used: {dict(level_counts)}")

            # The corrupted steps should have fallen to L2 or L3 (not L1)
            corrupted_labels = {
                "click_btn_save_layout",
                "click_curation_tab_schedule",
                "click_btn_save_schedule",
            }
            fallback_success = 0
            for step, result, lvl in results:
                if step.semantic_label in corrupted_labels:
                    info(
                        f"  corrupted step '{step.semantic_label}' "
                        f"-> level {lvl} ({'ok' if result.success else 'FAIL'})"
                    )
                    if result.success and lvl >= 2:
                        fallback_success += 1

            if fallback_success == len(corrupted_labels):
                info("RESILIENCE OK: all corrupted steps resolved via fallback")
                return 0
            info(
                f"RESILIENCE FAIL: {fallback_success}/{len(corrupted_labels)} "
                "corrupted steps recovered"
            )
            return 1
        finally:
            session.close()
    finally:
        stop_tree(chrome)
        stop_tree(vite)


if __name__ == "__main__":
    sys.exit(main())
