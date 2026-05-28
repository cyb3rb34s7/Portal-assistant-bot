"""Scenario 6 accordion_toggle: open the advanced-settings accordion
(aria-expanded false -> true). Recorded on draft B-2002. Replay; verify
aria-expanded ends true regardless of start state (toggle_state desired).
"""
import sys
from harness import CDP, BASE, console, connect_to_chrome, login_and_goto, make_recorder, pump, finalize_and_report

ASSET = "B-2002"


def main():
    session = connect_to_chrome(CDP, target_url_substring=BASE)
    page, status = login_and_goto(session, ASSET)
    rec = make_recorder(session, "accordion_open")
    pump(rec, page, 400)

    start = page.eval_on_selector(
        "[data-testid=accordion-advanced-toggle]", "e => e.getAttribute('aria-expanded')"
    )
    console.print(f"[magenta]accordion start aria-expanded={start}[/magenta]")
    # Ensure we record an OPEN transition (false -> true).
    if start == "true":
        page.dispatch_event("[data-testid=accordion-advanced-toggle]", "click")
        pump(rec, page, 400)

    console.print("[cyan]opening accordion (expand)[/cyan]")
    page.dispatch_event("[data-testid=accordion-advanced-toggle]", "click")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=accordion-advanced-toggle]')"
        ".getAttribute('aria-expanded') === 'true'",
        timeout=6000,
    )
    after = page.eval_on_selector(
        "[data-testid=accordion-advanced-toggle]", "e => e.getAttribute('aria-expanded')"
    )
    console.print(f"[green]accordion now aria-expanded={after}[/green]")
    pump(rec, page, 500)

    finalize_and_report(rec, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
