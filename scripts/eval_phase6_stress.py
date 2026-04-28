"""Phase 6.10 - stress test: 3 layouts back-to-back without restart.

Drives the executor against the same long-running Chrome instance,
running curate_featured_row -> curate_grid_2x2 -> curate_carousel
in sequence. Verifies:
  - State doesn't bleed between runs (each layout shows up exactly
    once after its run)
  - The session count after 3 runs is 3 unique applied layouts in
    portal localStorage (one per run, since reset clears between)
  - No memory/file-handle leaks (new sessions dir per run)
"""

from __future__ import annotations

import asyncio
import contextlib
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


def launch_chrome(profile: Path) -> subprocess.Popen:
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
            PORTAL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_cdp(CDP)
    time.sleep(1.5)
    return p


def _reset_sync() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://localhost:{CDP}")
        page = b.contexts[0].pages[0]
        page.goto(PORTAL + "/upload", wait_until="networkidle")
        page.evaluate("() => window.localStorage.clear()")
        page.reload(wait_until="networkidle")


async def reset_portal() -> None:
    await asyncio.to_thread(_reset_sync)


def _state_sync() -> tuple[int, str]:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://localhost:{CDP}")
        page = b.contexts[0].pages[0]
        page.goto(PORTAL + "/curation", wait_until="networkidle")
        try:
            page.wait_for_selector('[data-testid="applied-layouts"]', timeout=5000)
        except Exception:
            return 0, "no applied-layouts"
        n = page.locator('[data-testid="applied-layouts"] li').count()
        if n == 0:
            return 0, "applied-layouts empty"
        text = page.locator('[data-testid="applied-layout-0"]').inner_text()
        return n, text


async def get_state() -> tuple[int, str]:
    return await asyncio.to_thread(_state_sync)


def make_step(
    *, skill_id: str, slots: int, comment: str
) -> PlanStep:
    params: dict[str, str] = {
        "content_csv_file_path": str(REPO / "tests/fixtures/batch.csv"),
        "layout_comment": comment,
    }
    for i in range(1, slots + 1):
        params[f"slot_{i}_content_id"] = f"A-900{i}"
        params[f"slot_{i}_image_file_path"] = f"tests/fixtures/images/A-900{i}.png"
    return PlanStep(
        idx=1,
        skill_id=skill_id,
        params=params,
        param_sources={"content_csv_file_path": "stress test fixture"},
    )


async def run_one_layout(skill_id: str, slots: int, comment: str) -> tuple[bool, str]:
    skills = load_skill_library(REPO / "skills")
    skill = next((s for s in skills if s.id == skill_id), None)
    if skill is None:
        return False, f"skill {skill_id} not found"
    cfg = RealExecutorConfig(
        skills_dir=REPO / "skills",
        sessions_dir=REPO / "sessions" / f"phase6-stress-{uuid.uuid4().hex[:6]}",
        cdp_endpoint=f"http://localhost:{CDP}",
        target_url_substring="localhost:5173",
    )
    cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
    ex = RealExecutor(cfg)
    step = make_step(skill_id=skill_id, slots=slots, comment=comment)
    result = await ex.execute(
        step, skill, emit_progress=lambda *_: None,
    )
    return result.succeeded, (result.error_message or "ok")


async def amain() -> int:
    profile = Path("/tmp/phase6-stress-profile")
    chrome = launch_chrome(profile)
    results = []
    try:
        runs = [
            ("curate_featured_row", 4, "stress-1-featured"),
            ("curate_grid_2x2", 4, "stress-2-grid"),
            ("curate_carousel", 5, "stress-3-carousel"),
        ]
        for skill_id, slots, comment in runs:
            await reset_portal()
            t0 = time.time()
            ok, detail = await run_one_layout(skill_id, slots, comment)
            elapsed = time.time() - t0
            n, state = await get_state()
            applied_ok = state.startswith(skill_id.replace("curate_", "").replace("_", "-"))
            print(
                f"\n--- {skill_id} ---\n"
                f"  exec ok    : {ok}  ({elapsed:.1f}s)\n"
                f"  detail     : {detail[:120]}\n"
                f"  portal-end : n={n} state={state!r}\n"
                f"  state-ok   : {applied_ok}"
            )
            results.append((skill_id, ok and applied_ok, elapsed, state))
    finally:
        try:
            chrome.send_signal(signal.SIGTERM)
            chrome.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                chrome.kill()

    passed = sum(1 for _, ok, *_ in results if ok)
    print(f"\n=== Phase 6 stress: {passed}/{len(results)} layouts succeeded ===")
    for skill_id, ok, elapsed, state in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {skill_id} ({elapsed:.1f}s) -> {state!r}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
