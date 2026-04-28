"""Phase 3 — record the curation workflow as a replayable skill.

Pipeline driven by this single script:

  1. Launch headless Chromium with --remote-debugging-port=9222 pointed
     at the local sample portal.
  2. `pilot.teach.TeachRecorder.setup()` injects grabber.js + binding.
  3. We drive the Upload -> Curation -> Save -> Apply workflow on the
     same Page handle the recorder is observing. Every action fires
     the binding callback; the recorder writes a TraceEvent for each.
  4. `recorder._finalize()` writes meta.json + trace.jsonl.
  5. `pilot.annotate.run_annotate(..., auto=True)` heuristically derives
     a v1 skill JSON from the trace.
  6. We replay it with `pilot.skill_runner.run_skill_from_file` against
     a fresh Chromium to verify the recorded skill is repeatable.

After this script finishes, the repo has a fresh skill at
`skills/curate_layout.json`. Phase 4 (annotate-LLM) will enrich it with
v2 metadata.

Usage:
    .venv/bin/python scripts/teach_workflow.py \\
        --csv tests/fixtures/batch.csv --layout featured-row \\
        --comment "Spring drop hero" --skill-name curate_layout
"""

from __future__ import annotations

import argparse
import csv
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from pilot.annotate import run_annotate
from pilot.browser import connect_to_chrome
from pilot.skill_runner import run_skill_from_file
from pilot.teach import TeachRecorder


REPO_ROOT = Path(__file__).resolve().parent.parent
console = Console()


# ---------------------------------------------------------------------------
# Chromium lifecycle
# ---------------------------------------------------------------------------


def _find_chrome_binary() -> str:
    for cmd in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(cmd)
        if path:
            return path
    # Fall back to Playwright's bundled Chromium.
    for pattern in (
        ".cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        ".cache/ms-playwright/chromium-*/chrome-linux/chrome",
    ):
        candidates = list(Path.home().glob(pattern))
        if candidates:
            return str(sorted(candidates)[-1])
    raise RuntimeError(
        "no chrome/chromium binary found; install one or run "
        "`playwright install chromium`"
    )


def _wait_for_cdp(port: int, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"CDP not reachable on :{port} within {timeout_s}s")


def launch_chrome(
    port: int, profile_dir: Path, portal_url: str
) -> subprocess.Popen:
    binary = _find_chrome_binary()
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--headless=new",
        portal_url,
    ]
    console.print(f"[dim]launching chrome: {binary} (port={port})[/dim]")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_cdp(port)
    return proc


def kill_chrome(proc: subprocess.Popen) -> None:
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Workflow driver — same as simulate_operator.py but synchronous, using
# the recorder's Page handle so events get captured.
# ---------------------------------------------------------------------------


LAYOUT_SLOTS = {"grid-2x2": 4, "featured-row": 4, "carousel": 5}


def drive_workflow(
    page,  # playwright.sync_api.Page
    csv_path: Path,
    layout: str,
    comment: str,
) -> None:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    needed = LAYOUT_SLOTS[layout]
    assert len(rows) >= needed

    # Reset prior state for deterministic recordings.
    page.evaluate("() => window.localStorage.clear()")
    page.reload(wait_until="networkidle")
    # Re-inject won't be needed; recorder.setup uses add_init_script.
    page.wait_for_timeout(300)

    page.click('[data-testid="nav-upload"]')
    page.wait_for_selector('[data-testid="upload-page"]')
    page.set_input_files('[data-testid="input-csv-file"]', str(csv_path))
    page.wait_for_selector('[data-testid="contents-table"]')
    console.print(f"[op]  uploaded csv ({len(rows)} rows)")

    page.click('[data-testid="nav-curation"]')
    page.wait_for_selector('[data-testid="curation-page"]')
    page.click(f'[data-testid="layout-option-{layout}"]')
    page.wait_for_selector('[data-testid="layout-editor"]')
    console.print(f"[op]  selected layout {layout}")

    for slot_idx in range(1, needed + 1):
        row = rows[slot_idx - 1]
        content_id = row["content_id"]
        image_abspath = (REPO_ROOT / row["image_path"]).resolve()
        page.select_option(
            f'[data-testid="slot-{slot_idx}-content-select"]', value=content_id
        )
        page.set_input_files(
            f'[data-testid="slot-{slot_idx}-image-input"]', str(image_abspath)
        )
        page.wait_for_selector(f'[data-testid="slot-{slot_idx}-image-ok"]')
        console.print(f"[op]  slot {slot_idx} <- {content_id}")

    page.fill('[data-testid="input-comment"]', comment)
    page.click('[data-testid="btn-save-layout"]')
    page.wait_for_selector('[data-testid="status-saved"]')
    console.print("[op]  saved")
    page.click('[data-testid="btn-apply-layout"]')
    page.wait_for_selector('[data-testid="status-applied"]')
    console.print("[op]  applied")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--layout", required=True, choices=list(LAYOUT_SLOTS))
    p.add_argument("--comment", required=True)
    p.add_argument("--skill-name", default="curate_layout")
    p.add_argument("--portal-url", default="http://localhost:5173")
    p.add_argument("--cdp-port", type=int, default=9222)
    p.add_argument("--sessions-dir", default="sessions")
    p.add_argument("--skills-dir", default="skills")
    p.add_argument("--profile-dir", default="/tmp/curationpilot-teach-profile")
    p.add_argument(
        "--skip-replay", action="store_true",
        help="record + annotate but skip the replay verification step",
    )
    args = p.parse_args()

    csv_path = (REPO_ROOT / args.csv).resolve()
    if not csv_path.exists():
        console.print(f"[red]csv not found:[/red] {csv_path}")
        return 2

    sessions_dir = REPO_ROOT / args.sessions_dir
    skills_dir = REPO_ROOT / args.skills_dir
    sessions_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    chrome = launch_chrome(
        args.cdp_port,
        Path(args.profile_dir),
        args.portal_url,
    )
    try:
        # ---- Record ----
        session = connect_to_chrome(
            f"http://localhost:{args.cdp_port}",
            target_url_substring="localhost",
        )
        recorder = TeachRecorder(
            session=session,
            sessions_dir=sessions_dir,
            skill_name=args.skill_name,
            console=console,
            capture_screenshots=True,
        )
        recorder.setup()
        console.rule("[bold cyan]recording[/bold cyan]")
        try:
            drive_workflow(
                page=session.page,
                csv_path=csv_path,
                layout=args.layout,
                comment=args.comment,
            )
            # Brief drain so any in-flight binding callbacks land before
            # we call _finalize.
            session.page.wait_for_timeout(500)
            recorder._drain_pending()
        finally:
            recorder._finalize()
            session.close()

        session_id = recorder.session_id
        console.print(
            f"[green]recorded:[/green] {len(recorder._events)} events, "
            f"session={session_id}"
        )

        # ---- Annotate ----
        console.rule("[bold cyan]annotating[/bold cyan]")
        skill_path = run_annotate(
            session_id=session_id,
            skill_name=args.skill_name,
            description=f"Curate a {args.layout} layout from a CSV of contents",
            base_url=args.portal_url,
            portal="sample_portal",
            auto=True,
            sessions_dir=sessions_dir,
            skills_dir=skills_dir,
        )
        console.print(f"[green]annotated:[/green] {skill_path}")

        # ---- Replay verification ----
        if args.skip_replay:
            return 0

        # Reset portal state before replay so we observe it actually
        # working from scratch (no leftover state from the recording).
        session2 = connect_to_chrome(
            f"http://localhost:{args.cdp_port}",
            target_url_substring="localhost",
        )
        try:
            session2.page.evaluate("() => window.localStorage.clear()")
            session2.page.reload(wait_until="networkidle")
        finally:
            session2.close()

        console.rule("[bold cyan]replaying[/bold cyan]")
        # Build params using the heuristic names the auto-annotator
        # produced (input_csv_file, slot_N_content_select,
        # slot_N_image_input, input_comment). Phase 4's LLM pass will
        # rename these to semantic names later; the replay here just
        # exercises the recorded trace with the same CSV.
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
        replay_params: dict[str, str] = {
            "input_csv_file": str(csv_path),
            "input_comment": args.comment,
        }
        for i, r in enumerate(rows[: LAYOUT_SLOTS[args.layout]], start=1):
            replay_params[f"slot_{i}_content_select"] = r["content_id"]
            replay_params[f"slot_{i}_image_input"] = str(
                (REPO_ROOT / r["image_path"]).resolve()
            )
        results = run_skill_from_file(
            skill_path=skill_path,
            params=replay_params,
            base_url=args.portal_url,
            cdp=f"http://localhost:{args.cdp_port}",
            sessions_dir=sessions_dir,
        )
        failed = [r for (_, r, _) in results if not r.success]
        if failed:
            console.print(
                f"[red]replay had {len(failed)} failed step(s)[/red]"
            )
            return 1
        console.print(
            f"[green]replay ok:[/green] {len(results)} steps, all green"
        )
        return 0
    finally:
        kill_chrome(chrome)


if __name__ == "__main__":
    sys.exit(main())
