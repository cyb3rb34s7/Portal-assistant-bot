"""Scenario 7 workflow_submit (DESTRUCTIVE, last): click "Submit for review".
Recorded on draft C-3001. Replay on a fresh draft (D-4001). Verify status
-> in_review. Tests a destructive transition + _robust_click.
"""
import sys
from harness import CDP, BASE, console, connect_to_chrome, login_and_goto, make_recorder, pump, finalize_and_report

ASSET = "C-3001"


def main():
    session = connect_to_chrome(CDP, target_url_substring=BASE)
    page, status = login_and_goto(session, ASSET)
    rec = make_recorder(session, "workflow_submit")
    pump(rec, page, 400)

    console.print("[cyan]clicking Submit for review[/cyan]")
    page.dispatch_event("[data-testid=btn-submit-review]", "click")
    # Real ~2-3s transition: wait for the status pill to flip to in_review.
    page.wait_for_function(
        "() => document.querySelector('[data-testid=asset-status]') && "
        "document.querySelector('[data-testid=asset-status]').textContent.trim() === 'in_review'",
        timeout=12000,
    )
    after = page.eval_on_selector("[data-testid=asset-status]", "e => e.textContent.trim()")
    console.print(f"[green]status now: {after}[/green]")
    pump(rec, page, 800)

    finalize_and_report(rec, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
