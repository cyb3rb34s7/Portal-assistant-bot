"""Shared E2E record harness helpers (2026-05-28 round).

Models exactly on tmp/layerb_record.py (the proven green pattern):
  connect -> login -> goto draft asset -> TeachRecorder.setup()
  -> drive operator-sim actions with dispatch_event/fill + pump drains
  -> _finalize() -> print SESSION_ID + EVENT_KINDS.

Operator-sim clicks MUST use page.dispatch_event(sel,"click") because on
this CDP-attached Chrome (v148) Playwright real-mouse clicks NO-OP for
React onClick. For native <select> we set value + dispatch change/input.
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console

from pilot.browser import connect_to_chrome
from pilot.teach import TeachRecorder

CDP = "http://127.0.0.1:9222"
BASE = "http://localhost:5188"
console = Console()


def login_and_goto(session, asset: str):
    page = session.page
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    if page.query_selector("[data-testid=login-form]") or page.query_selector(
        "input[type=password]"
    ):
        u = page.query_selector("input:not([type=password])")
        p = page.query_selector("input[type=password]")
        if u and p:
            u.fill("operator")
            p.fill("test123")
            page.eval_on_selector("form", "f => f.requestSubmit()")
            try:
                page.wait_for_url("**/catalog", timeout=10000)
                console.print("[green]logged in[/green]")
            except Exception:
                console.print("[yellow]login wait timed out (maybe already authed)[/yellow]")
    page.goto(f"{BASE}/asset/{asset}", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=asset-edit-form]", timeout=10000)
    status = page.eval_on_selector(
        "[data-testid=asset-status]", "e => e.textContent.trim()"
    )
    console.print(f"asset {asset} status: {status}")
    return page, status


def make_recorder(session, skill_name: str) -> TeachRecorder:
    rec = TeachRecorder(
        session=session,
        sessions_dir=Path("sessions"),
        skill_name=skill_name,
        console=console,
        capture_screenshots=False,
        portal_id="sample_portal",
    )
    rec.setup()
    session.page.wait_for_selector("[data-testid=asset-edit-form]", timeout=10000)
    return rec


def pump(recorder, page, ms=400):
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass
    try:
        recorder._drain_pending()
    except Exception as e:
        console.print(f"[yellow]drain: {e}[/yellow]")


def finalize_and_report(recorder, session):
    page = session.page
    for _ in range(3):
        pump(recorder, page, 250)
    recorder._finalize()
    console.print(f"[bold green]SESSION_ID={recorder.session_id}[/bold green]")
    console.print(f"events={len(recorder._events)}")
    kinds = [
        (e.kind, getattr(e.fingerprint, "test_id", None), getattr(e, "value", None))
        for e in recorder._events
    ]
    console.print(f"[bold]EVENT_KINDS={kinds}[/bold]")
    session.close()
