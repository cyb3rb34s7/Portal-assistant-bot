"""End-to-end test harness for the CurationPilot POC.

Starts the sample portal (vite), launches Chrome with CDP, drives the
portal through Playwright to produce captured events, runs the
annotator in --auto mode, then replays the resulting skill with
different parameters.

Run with:
    .venv/Scripts/python.exe scripts/e2e_test.py
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
    print(f"[e2e] {msg}", flush=True)


def wait_for(url: str, timeout: float = 30.0, label: str = "") -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    info(f"timed out waiting for {label or url}")
    return False


def start_portal() -> subprocess.Popen:
    info("starting vite dev server ...")
    proc = subprocess.Popen(
        ["npm.cmd", "run", "dev"],
        cwd=str(PORTAL_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    if not wait_for(PORTAL_URL, timeout=45.0, label="portal"):
        proc.kill()
        raise RuntimeError("portal did not come up")
    info("portal is up")
    return proc


def start_chrome() -> subprocess.Popen:
    info("launching Chrome with CDP ...")
    if CHROME_PROFILE.exists():
        shutil.rmtree(CHROME_PROFILE, ignore_errors=True)
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        str(CHROME_BIN),
        "--remote-debugging-port=9222",
        f"--user-data-dir={CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        PORTAL_URL,
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    if not wait_for(CDP_URL + "/json/version", timeout=30.0, label="CDP"):
        proc.kill()
        raise RuntimeError("CDP endpoint did not come up")
    info("Chrome CDP is up")
    return proc


def stop_process_tree(proc: subprocess.Popen, name: str) -> None:
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


def drive_curation_flow(session) -> None:
    """Simulate an operator building out one content row end-to-end."""
    page = session.page
    page.wait_for_load_state("networkidle", timeout=10000)
    info("driving: navigate to Curation")
    page.get_by_test_id("nav-curation").click()
    page.wait_for_selector("[data-testid='page-curation']", timeout=5000)

    info("driving: fill layout form")
    page.get_by_test_id("input-layout-content-id").click()
    page.get_by_test_id("input-layout-content-id").fill("A-9001")
    page.get_by_test_id("input-layout-row").fill("3")
    page.get_by_test_id("input-layout-position").fill("2")
    page.get_by_test_id("btn-save-layout").click()
    page.wait_for_timeout(400)

    info("driving: switch to Schedule tab")
    page.get_by_test_id("curation-tab-schedule").click()
    page.get_by_test_id("input-schedule-content-id").fill("A-9001")
    page.get_by_test_id("input-schedule-start").fill("2026-05-01")
    page.get_by_test_id("input-schedule-end").fill("2026-05-31")
    page.get_by_test_id("btn-save-schedule").click()
    page.wait_for_timeout(400)

    info("driving: switch to Thumbnails tab")
    page.get_by_test_id("curation-tab-thumbnails").click()
    page.get_by_test_id("input-thumb-content-id").fill("A-9001")
    page.wait_for_timeout(400)
    # Deliberately skip the file upload here — the recorder will capture
    # the content-id fill, and replay will use whatever param we pass.

    info("driving: done")


def print_trace_summary(session_dir: Path) -> None:
    trace = session_dir / "trace.jsonl"
    if not trace.exists():
        info(f"NO TRACE AT {trace}")
        return
    lines = trace.read_text(encoding="utf-8").splitlines()
    info(f"trace has {len(lines)} events")
    for i, line in enumerate(lines[:30]):
        try:
            obj = json.loads(line)
            fp = obj.get("fingerprint") or {}
            label = (
                fp.get("test_id")
                or fp.get("accessible_name")
                or fp.get("text")
                or fp.get("tag")
                or ""
            )
            val = obj.get("value") or obj.get("file_name") or obj.get("url") or ""
            info(f"  {i:03d}  {obj.get('kind'):14s}  {label[:40]:40s}  {str(val)[:30]}")
        except Exception as e:
            info(f"  {i:03d}  <parse error: {e}>")


def main() -> int:
    SESSIONS_DIR.mkdir(exist_ok=True)
    SKILLS_DIR.mkdir(exist_ok=True)

    vite_proc = start_portal()
    chrome_proc = start_chrome()

    try:
        from pilot.browser import connect_to_chrome
        from pilot.teach import TeachRecorder
        from pilot.annotate import run_annotate
        from pilot.skill_runner import SkillRunner
        from pilot.skill_models import Skill

        info("connecting via CDP ...")
        session = connect_to_chrome(CDP_URL, target_url_substring=PORTAL_URL)

        try:
            # ---------- TEACH ----------
            info("=== TEACH PHASE ===")
            recorder = TeachRecorder(
                session=session,
                sessions_dir=SESSIONS_DIR,
                skill_name="curate_one_item",
                capture_screenshots=False,   # speed up test
            )
            recorder.setup()
            info(f"teach session id: {recorder.session_id}")

            # Drive actions
            drive_curation_flow(session)

            # Blur the active element so any in-flight debounce flushes,
            # then give it time to hit the binding callback.
            try:
                session.page.evaluate("document.activeElement && document.activeElement.blur()")
            except Exception:
                pass
            session.page.wait_for_timeout(800)

            # Finalize
            recorder._finalize()
            session_dir = SESSIONS_DIR / recorder.session_id
            print_trace_summary(session_dir)

            # ---------- ANNOTATE ----------
            info("=== ANNOTATE PHASE ===")
            out = run_annotate(
                session_id=recorder.session_id,
                skill_name="curate_one_item",
                description="Create layout+schedule for a content item",
                base_url=PORTAL_URL,
                portal="sample_portal",
                auto=True,
                sessions_dir=SESSIONS_DIR,
                skills_dir=SKILLS_DIR,
            )
            skill = Skill.model_validate_json(out.read_text(encoding="utf-8"))
            info(f"skill saved: {out}")
            info(f"declared params: {[p.name for p in skill.params]}")
            info(f"steps: {len(skill.steps)}")

            # ---------- REPLAY ----------
            info("=== REPLAY PHASE ===")
            # Reset state: navigate to dashboard first so replay starts clean
            session.page.goto(PORTAL_URL + "/dashboard")
            session.page.wait_for_timeout(300)

            # Build params: override content_id to a new value, reuse date values
            replay_params = {p.name: (p.example or "") for p in skill.params}
            # Swap the content id param value
            for p in skill.params:
                if "content" in p.name.lower():
                    replay_params[p.name] = "A-9042"
            info(f"replay params: {replay_params}")

            runner = SkillRunner(
                session=session,
                skill=skill,
                params=replay_params,
                sessions_dir=SESSIONS_DIR,
                base_url=PORTAL_URL,
                approve_fn=lambda step: True,          # auto-approve gates in test
                takeover_fn=lambda step: False,        # don't block on takeover
            )
            results = runner.run()

            fails = [(s, r, lvl) for (s, r, lvl) in results if not r.success]
            info(f"replay finished: {len(results)} steps, {len(fails)} failures")
            if fails:
                for s, r, lvl in fails:
                    info(f"  FAIL step {s.index} {s.semantic_label}: {r.error}")

            # ---------- VERIFICATION ----------
            info("=== VERIFICATION PHASE ===")
            # IMPORTANT: don't page.goto here — the sample portal uses
            # in-memory React state that resets on hard reload. Use SPA
            # navigation (sidebar + tab click) so the replay's saved
            # state is preserved.
            session.page.wait_for_timeout(300)
            info(f"post-replay URL: {session.page.url}")
            try:
                active = session.page.locator(".curation-tab.active").first.inner_text()
                info(f"active sub-tab before verify: {active!r}")
            except Exception as e:
                info(f"could not read active sub-tab: {e}")

            # Take a screenshot so we can see what actually happened
            shot_path = SESSIONS_DIR / "verification_before_tab_click.png"
            try:
                session.page.screenshot(path=str(shot_path))
                info(f"screenshot: {shot_path}")
            except Exception as e:
                info(f"screenshot failed: {e}")

            session.page.get_by_test_id("curation-tab-layout").click()
            session.page.wait_for_timeout(200)

            # The recorded flow left Thumbnails dirty (typed content_id but
            # never saved), so switching tabs opens the dirty-guard modal.
            # Discard and continue — we want to verify prior saved tabs.
            modal = session.page.locator("[data-testid='dirty-guard-modal']")
            if modal.count() > 0 and modal.first.is_visible():
                info("dirty-guard modal appeared — discarding to switch tab")
                session.page.get_by_test_id("btn-dirty-discard").click()
                session.page.wait_for_timeout(200)

            rows = session.page.locator("[data-testid^='layout-row-']").all()
            ids = [r.get_attribute("data-testid") for r in rows]
            info(f"layout rows in DOM: {ids}")

            expected_id = f"layout-row-A-9042"
            if expected_id not in ids:
                info(f"VERIFICATION FAIL: expected {expected_id} in layout table")
                return 2
            info("VERIFICATION OK: replayed row is present")

            # Also verify the schedule tab
            session.page.get_by_test_id("curation-tab-schedule").click()
            session.page.wait_for_timeout(200)
            modal = session.page.locator("[data-testid='dirty-guard-modal']")
            if modal.count() > 0 and modal.first.is_visible():
                session.page.get_by_test_id("btn-dirty-discard").click()
                session.page.wait_for_timeout(200)
            sched_rows = session.page.locator("[data-testid^='schedule-row-']").all()
            sched_ids = [r.get_attribute("data-testid") for r in sched_rows]
            info(f"schedule rows in DOM: {sched_ids}")
            if f"schedule-row-A-9042" not in sched_ids:
                info("VERIFICATION FAIL: expected schedule-row-A-9042")
                return 2
            info("VERIFICATION OK: replayed schedule is present")

            exit_code = 0 if not fails else 1
            return exit_code
        finally:
            session.close()
    finally:
        info("stopping Chrome ...")
        stop_process_tree(chrome_proc, "chrome")
        info("stopping vite ...")
        stop_process_tree(vite_proc, "vite")


if __name__ == "__main__":
    sys.exit(main())
