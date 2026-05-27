"""Layer B record harness — records a multi-select-with-search selection
through the real TeachRecorder (grabber.js injected over CDP), driving the
"operator" actions via Playwright so the grabber captures genuine DOM
events. Writes a trace; annotate + run-skill happen as separate CLI steps.

Scenario: open the Country picker, search "Argentina", select it.
(Replay will target a DIFFERENT country to prove the search-by-label +
visibility-gated reach.)
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

from pilot.browser import connect_to_chrome
from pilot.teach import TeachRecorder

CDP = "http://127.0.0.1:9222"
BASE = "http://localhost:5188"
ASSET = "A-1001"
console = Console()


def pump(recorder, page, ms=400):
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass
    try:
        recorder._drain_pending()
    except Exception as e:
        console.print(f"[yellow]drain: {e}[/yellow]")


def main() -> int:
    session = connect_to_chrome(CDP, target_url_substring=BASE)
    page = session.page

    # 1) Log in (fresh Chrome profile -> not authenticated).
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    if page.query_selector("[data-testid=login-form]"):
        # Fill via real typing so React's controlled inputs register.
        u = page.query_selector("input:not([type=password])")
        p = page.query_selector("input[type=password]")
        u.fill("operator")
        p.fill("test123")
        page.eval_on_selector("form", "f => f.requestSubmit()")
        page.wait_for_url("**/catalog", timeout=8000)
        console.print("[green]logged in[/green]")

    # 2) Navigate to a draft asset (the picker is editable only on draft).
    page.goto(f"{BASE}/asset/{ASSET}", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=asset-edit-form]", timeout=8000)
    status = page.eval_on_selector(
        "[data-testid=asset-status]", "e => e.textContent.trim()"
    )
    console.print(f"asset {ASSET} status: {status}")

    # 3) Start the recorder (injects grabber + binding, reloads page).
    recorder = TeachRecorder(
        session=session,
        sessions_dir=Path("sessions"),
        skill_name="country_pick",
        console=console,
        capture_screenshots=False,
        portal_id="sample_portal",
    )
    recorder.setup()
    # Reload may re-load the asset page; re-wait for the form.
    page.wait_for_selector("[data-testid=asset-edit-form]", timeout=8000)
    pump(recorder, page, 400)

    # 4) Drive the Country multi-select via search (the operator's actions).
    # Diagnostic: is the country picker present + enabled?
    diag = page.evaluate(
        "() => {const t=document.querySelector('[data-testid=multiselect-country-toggle]');"
        "return JSON.stringify({toggle:!!t, disabled:t?t.disabled:null, "
        "ids:Array.from(document.querySelectorAll('[data-testid]')).map(e=>e.getAttribute('data-testid')).filter(x=>x.includes('country')).slice(0,8)});}"
    )
    console.print(f"[magenta]picker diag: {diag}[/magenta]")

    # NOTE: operator-sim actions use dispatch_event('click') because on
    # this CDP-attached Chrome (v148) Playwright's synthesized real-mouse
    # clicks do NOT fire React's onClick handlers (2026-05-22 finding);
    # dispatch_event reaches the handler so the grabber records genuine
    # DOM events.
    console.print("[cyan]opening country picker[/cyan]")
    page.dispatch_event("[data-testid=multiselect-country-toggle]", "click")
    pump(recorder, page, 700)
    after = page.evaluate(
        "() => JSON.stringify({popover:!!document.querySelector('[data-testid=multiselect-country-popover]'),"
        "search:!!document.querySelector('[data-testid=multiselect-country-search]')})"
    )
    console.print(f"[magenta]after toggle: {after}[/magenta]")

    console.print("[cyan]searching 'Argentina'[/cyan]")
    page.fill("[data-testid=multiselect-country-search]", "Argentina")
    # Server-side search: wait for the filtered option to render.
    page.wait_for_selector(
        "[data-testid=multiselect-country-checkbox-ar]", timeout=8000
    )
    pump(recorder, page, 600)

    console.print("[cyan]selecting Argentina[/cyan]")
    page.dispatch_event("[data-testid=multiselect-country-checkbox-ar]", "click")
    pump(recorder, page, 500)

    # Close the picker.
    page.dispatch_event("[data-testid=multiselect-country-toggle]", "click")
    pump(recorder, page, 400)

    # Final drains + write trace.
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
