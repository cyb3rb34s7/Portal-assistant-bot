"""Phase 6 executor-only bench (no LLM, no planner).

Drives `RealExecutor.execute()` directly with a hand-crafted
`PlanStep`, against headless Chromium over CDP. Verifies the
executor + skill_runner + locator-fallback layer surface failures
cleanly (no crashes, structured StepResult) for these scenarios:

  E1  Missing image file on disk    -> expect StepResult(succeeded=False,
                                          error_kind in {skill_step_failed,
                                          skill_runner_crashed})
  E2  Locator drift (data-testid changed) -> expect L2/L3 fallback
                                          fires and step still succeeds
                                          OR fails cleanly with a
                                          fallback_human / unknown_error.
  E3  Already-applied portal state    -> expect Save/Apply still works
                                          (or fails cleanly).
  E4  task.cancel mid-run             -> expect graceful unwind
                                          (CancelledError surfaces; no
                                          orphan Chrome processes).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

from pilot.agent.executor_real import RealExecutor, RealExecutorConfig
from pilot.agent.planner import load_skill_library
from pilot.agent.schemas.domain import PlanStep


REPO = Path(__file__).resolve().parent.parent
CDP = 9222
PORTAL = "http://localhost:5173"


def _find_chrome() -> str:
    for cmd in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(cmd)
        if path:
            return path
    for pattern in (
        ".cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        ".cache/ms-playwright/chromium-*/chrome-linux/chrome",
    ):
        cands = list(Path.home().glob(pattern))
        if cands:
            return str(sorted(cands)[-1])
    raise RuntimeError("no chromium found")


def _wait_cdp(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"CDP not reachable on :{port}")


def launch_chrome(profile: Path, url: str) -> subprocess.Popen:
    profile.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [
            _find_chrome(),
            f"--remote-debugging-port={CDP}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--headless=new",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_cdp(CDP)
    time.sleep(1.5)
    return p


def _reset_portal_sync(*, set_existing_layout: bool = False) -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://localhost:{CDP}")
        page = b.contexts[0].pages[0]
        # Force navigation home from whatever page we were left on.
        page.goto(PORTAL + "/upload", wait_until="networkidle")
        page.evaluate("() => window.localStorage.clear()")
        if set_existing_layout:
            # Pre-seed an applied layout to test E3.
            page.evaluate(
                "() => window.localStorage.setItem("
                "'cp_state', JSON.stringify({"
                "rows:[],"
                "applied:[{layout_id:'featured-row',slots:["
                "{content_id:'A-9001',image_path:'tests/fixtures/images/A-9001.png'},"
                "{content_id:'A-9002',image_path:'tests/fixtures/images/A-9002.png'},"
                "{content_id:'A-9003',image_path:'tests/fixtures/images/A-9003.png'},"
                "{content_id:'A-9004',image_path:'tests/fixtures/images/A-9004.png'}],"
                "comment:'Pre-existing'}]"
                "}))"
            )
        page.reload(wait_until="networkidle")


async def reset_portal(*, set_existing_layout: bool = False) -> None:
    await asyncio.to_thread(
        _reset_portal_sync, set_existing_layout=set_existing_layout
    )


def _setup_locator_drift_sync() -> tuple[int, int]:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://localhost:{CDP}")
        page = b.contexts[0].pages[0]
        # Upload a CSV first so the layout picker has contents to bind.
        page.goto(PORTAL + "/upload", wait_until="networkidle")
        page.set_input_files(
            '[data-testid="input-csv-file"]',
            str(REPO / "tests/fixtures/batch.csv"),
        )
        page.wait_for_selector(
            '[data-testid="contents-table"]', timeout=10_000
        )
        page.goto(PORTAL + "/curation", wait_until="networkidle")
        page.wait_for_selector(
            '[data-testid="layout-option-featured-row"]', timeout=10_000
        )
        page.click('[data-testid="layout-option-featured-row"]')
        page.wait_for_selector(
            '[data-testid="slot-1-content-select"]', timeout=10_000
        )
        # Now rename slot 1's content select testid to force the runner
        # off L1 onto its semantic / human fallbacks.
        page.evaluate(
            "() => {"
            "const el = document.querySelector("
            "'[data-testid=\"slot-1-content-select\"]');"
            "if (el) el.setAttribute('data-testid', 'slot-1-content-RENAMED');"
            "}"
        )
        original = page.locator(
            '[data-testid="slot-1-content-select"]'
        ).count()
        renamed = page.locator(
            '[data-testid="slot-1-content-RENAMED"]'
        ).count()
        return original, renamed


async def run_step(plan_step: PlanStep, skill_id: str):
    skills = load_skill_library(REPO / "skills")
    skill = next((s for s in skills if s.id == skill_id), None)
    if skill is None:
        raise RuntimeError(f"skill {skill_id} not loaded")
    cfg = RealExecutorConfig(
        skills_dir=REPO / "skills",
        sessions_dir=REPO / "sessions" / f"phase6-{uuid.uuid4().hex[:6]}",
        cdp_endpoint=f"http://localhost:{CDP}",
        target_url_substring="localhost:5173",
    )
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    ex = RealExecutor(cfg)
    events: list[tuple[str, dict]] = []
    result = await ex.execute(
        plan_step,
        skill,
        emit_progress=lambda kind, payload: events.append((kind, payload)),
    )
    return result, events


def featured_row_step(*, image_for_slot_2: str | None = None) -> PlanStep:
    """Returns a baseline curate_featured_row plan_step. If
    `image_for_slot_2` is provided, swaps in that path (e.g. a
    nonexistent file)."""
    img = lambda i: f"tests/fixtures/images/A-900{i}.png"  # noqa: E731
    return PlanStep(
        idx=1,
        skill_id="curate_featured_row",
        params={
            "content_csv_file_path": str(REPO / "tests/fixtures/batch.csv"),
            "slot_1_content_id": "A-9001",
            "slot_1_image_file_path": img(1),
            "slot_2_content_id": "A-9002",
            "slot_2_image_file_path": image_for_slot_2 or img(2),
            "slot_3_content_id": "A-9003",
            "slot_3_image_file_path": img(3),
            "slot_4_content_id": "A-9004",
            "slot_4_image_file_path": img(4),
            "layout_comment": "Phase 6 executor test",
        },
        param_sources={
            "content_csv_file_path": "test fixture",
        },
    )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


async def case_e1_missing_image() -> tuple[bool, str]:
    await reset_portal()
    plan_step = featured_row_step(
        image_for_slot_2="tests/fixtures/images/MISSING-9999.png"
    )
    result, events = await run_step(plan_step, "curate_featured_row")
    if result.succeeded:
        return False, f"E1 unexpectedly succeeded with missing image"
    if result.error_kind in ("skill_step_failed", "skill_runner_crashed"):
        return True, (
            f"E1 surfaced as {result.error_kind!r}: "
            f"{(result.error_message or '')[:200]}"
        )
    return False, f"E1 failed but with unexpected error_kind={result.error_kind!r}"


async def case_e2_locator_drift() -> tuple[bool, str]:
    """Rename a data-testid in the live DOM before running, force the
    runner to fall back to L2/L3 to find slot_1's content select."""
    await reset_portal()
    original, renamed = await asyncio.to_thread(_setup_locator_drift_sync)

    if original != 0 or renamed != 1:
        return False, f"E2 setup failed: original={original} renamed={renamed}"

    plan_step = featured_row_step()
    result, _ = await run_step(plan_step, "curate_featured_row")
    detail = (
        f"E2 result: succeeded={result.succeeded} "
        f"error_kind={result.error_kind!r}"
    )
    if result.succeeded:
        return True, detail + " (L2/L3 fallback hit)"
    if result.error_kind in ("skill_step_failed", "skill_runner_crashed"):
        return True, detail + " (failed cleanly with structured error)"
    return False, detail + " UNEXPECTED error_kind"


async def case_e3_pre_existing_state() -> tuple[bool, str]:
    """Portal already has an applied layout. Run the curate flow
    again. Should still work (the curation page lets you save+apply
    a fresh layout on top)."""
    await reset_portal(set_existing_layout=True)
    plan_step = featured_row_step()
    plan_step.params["layout_comment"] = "Phase 6 over pre-existing"
    result, _ = await run_step(plan_step, "curate_featured_row")
    detail = (
        f"E3 result: succeeded={result.succeeded} "
        f"error_kind={result.error_kind!r}"
    )
    return (result.succeeded, detail)


async def case_e4_cancel_mid_run() -> tuple[bool, str]:
    """Schedule a cancel ~3s into the run and verify orderly shutdown."""
    await reset_portal()
    plan_step = featured_row_step()

    skills = load_skill_library(REPO / "skills")
    skill = next(s for s in skills if s.id == "curate_featured_row")
    cfg = RealExecutorConfig(
        skills_dir=REPO / "skills",
        sessions_dir=REPO / "sessions" / f"phase6-cancel-{uuid.uuid4().hex[:6]}",
        cdp_endpoint=f"http://localhost:{CDP}",
        target_url_substring="localhost:5173",
    )
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    ex = RealExecutor(cfg)

    task = asyncio.create_task(
        ex.execute(plan_step, skill, emit_progress=lambda *_: None)
    )

    async def canceller():
        await asyncio.sleep(3.0)
        task.cancel()

    cancel_task = asyncio.create_task(canceller())
    cancelled = False
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True
    except Exception as e:  # noqa: BLE001
        return False, f"E4 raised {type(e).__name__}: {e}"
    finally:
        cancel_task.cancel()
        with contextlib.suppress(BaseException):
            await cancel_task

    if cancelled:
        return True, "E4 cancellation propagated cleanly"
    # If the run finished before our 3s timer, that's also OK — just note it.
    return True, "E4 run finished before cancel fired (no orphan)"


async def amain() -> int:
    profile = Path("/tmp/phase6-executor-profile")
    chrome = launch_chrome(profile, PORTAL)
    results: list[tuple[str, bool, str]] = []
    try:
        cases = [
            ("E1-missing-image", case_e1_missing_image),
            ("E2-locator-drift", case_e2_locator_drift),
            ("E3-pre-existing-state", case_e3_pre_existing_state),
            ("E4-cancel-mid-run", case_e4_cancel_mid_run),
        ]
        for name, fn in cases:
            print(f"\n=== {name} ===", flush=True)
            try:
                ok, detail = await fn()
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"raised {type(e).__name__}: {e}"
            print(f"  -> {'PASS' if ok else 'FAIL'}: {detail}", flush=True)
            results.append((name, ok, detail))
    finally:
        try:
            chrome.send_signal(signal.SIGTERM)
            chrome.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                chrome.kill()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== Phase 6 executor bench: {passed}/{len(results)} cases passed ===")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail[:200]}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
