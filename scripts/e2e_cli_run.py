"""Phase 5 - end-to-end CLI run with real Groq + real Chrome.

Procedure:
  1. Start Chromium with --remote-debugging-port=9222 pointed at the
     local sample portal.
  2. Reset portal localStorage so the run starts clean.
  3. Invoke `python -m pilot.agent.cli do <goal> --client groq
     --executor real --portal sample_portal --auto-approve` against
     the live agent + planner stack.
  4. Verify the portal ended up with an applied layout matching the goal.

This script is the canonical "does the whole product actually work?"
test for v1. Real LLM, real browser, real recorded skill, real CDP.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright


REPO = Path(__file__).resolve().parent.parent


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


def launch_chrome(port: int, profile: Path, url: str) -> subprocess.Popen:
    profile.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [
            _find_chrome(),
            f"--remote-debugging-port={port}",
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
    _wait_cdp(port)
    # Give the React app a moment to mount before we touch localStorage.
    time.sleep(1.5)
    return p


def reset_portal(port: int, url: str) -> None:
    """Clear localStorage on the headless tab so the run starts clean."""
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://localhost:{port}")
        page = b.contexts[0].pages[0]
        page.goto(url, wait_until="networkidle")
        page.evaluate("() => window.localStorage.clear()")
        page.reload(wait_until="networkidle")
        # Don't close the browser - we just disconnect the playwright driver.


def assert_applied(port: int, url: str) -> tuple[bool, str]:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://localhost:{port}")
        page = b.contexts[0].pages[0]
        page.goto(url + "/curation", wait_until="networkidle")
        try:
            page.wait_for_selector('[data-testid="applied-layouts"]', timeout=5000)
        except Exception:
            return False, "applied-layouts list never appeared"
        n = page.locator('[data-testid="applied-layouts"] li').count()
        if n == 0:
            return False, "applied-layouts list is empty"
        text = page.locator('[data-testid="applied-layout-0"]').inner_text()
        return True, text


def run_agent_cli(
    goal: str,
    csv_path: Path,
    client: str,
    cdp_port: int,
    timeout_s: int,
    *,
    llm_intake: bool = False,
    extra_args: list[str] | None = None,
) -> tuple[int, str]:
    cmd = [
        sys.executable,
        "-m", "pilot.agent.cli",
        "do",
        goal,
        "--attach", str(csv_path),
        "--client", client,
        "--portal", "sample_portal",
        "--executor", "real",
        "--auto-approve",
        "--cdp-endpoint", f"http://localhost:{cdp_port}",
        "--target-url", "localhost:5173",
    ]
    if not llm_intake:
        cmd.append("--no-llm-intake")
    if extra_args:
        cmd.extend(extra_args)
    print(f"\n>>> {' '.join(cmd)}\n", flush=True)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=str(REPO),
    )
    out = proc.stdout + "\n--STDERR--\n" + proc.stderr
    return proc.returncode, out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="tests/fixtures/batch.csv")
    p.add_argument(
        "--goal",
        default=(
            "Curate a featured-row layout from this CSV. "
            "Comment: 'E2E run from CLI'."
        ),
    )
    p.add_argument("--portal-url", default="http://localhost:5173")
    p.add_argument("--cdp-port", type=int, default=9222)
    p.add_argument("--client", default="groq")
    p.add_argument(
        "--profile-dir",
        default="/tmp/curationpilot-e2e-profile",
    )
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument(
        "--llm-intake",
        action="store_true",
        help="Use the LLM intake stage (drop --no-llm-intake).",
    )
    p.add_argument(
        "--extra-cli-arg",
        action="append",
        default=[],
        help="Extra args appended to the agent CLI invocation (repeatable).",
    )
    args = p.parse_args()

    csv_path = (REPO / args.csv).resolve()
    if not csv_path.exists():
        print(f"FATAL: csv not found: {csv_path}", file=sys.stderr)
        return 2

    chrome = launch_chrome(args.cdp_port, Path(args.profile_dir), args.portal_url)
    try:
        reset_portal(args.cdp_port, args.portal_url)

        rc, log = run_agent_cli(
            args.goal, csv_path, args.client, args.cdp_port, args.timeout,
            llm_intake=args.llm_intake,
            extra_args=args.extra_cli_arg,
        )
        # Print the agent CLI output regardless of outcome.
        print("=" * 70)
        print(log[-6000:])
        print("=" * 70)

        if rc != 0:
            print(f"FAIL: agent CLI exited with code {rc}")
            return rc

        ok, detail = assert_applied(args.cdp_port, args.portal_url)
        if ok:
            print(f"\nSUCCESS: applied layout visible on portal: {detail!r}")
            return 0
        print(f"\nFAIL: portal end-state check: {detail}")
        return 1
    finally:
        try:
            chrome.send_signal(signal.SIGTERM)
            chrome.wait(timeout=5)
        except Exception:
            try:
                chrome.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
