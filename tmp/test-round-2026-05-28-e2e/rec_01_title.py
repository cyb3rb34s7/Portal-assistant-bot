"""Scenario 1 title_edit: set title -> click Save.
Record value "Recorded Title" on draft A-9001. Replay with "Replayed Title".
"""
import sys
from harness import CDP, BASE, console, connect_to_chrome, login_and_goto, make_recorder, pump, finalize_and_report

ASSET = "A-9001"


def main():
    session = connect_to_chrome(CDP, target_url_substring=BASE)
    page, status = login_and_goto(session, ASSET)
    rec = make_recorder(session, "title_edit")
    pump(rec, page, 400)

    console.print("[cyan]typing title[/cyan]")
    page.fill("[data-testid=input-asset-title]", "Recorded Title")
    pump(rec, page, 500)

    console.print("[cyan]clicking Save[/cyan]")
    page.dispatch_event("[data-testid=btn-save]", "click")
    # Real ~2-4s write: wait for the Saved toast as the success signal.
    try:
        page.wait_for_selector("[data-testid=toast-success]", timeout=12000)
        console.print("[green]Saved toast observed[/green]")
    except Exception as e:
        console.print(f"[yellow]no toast: {e}[/yellow]")
    pump(rec, page, 800)

    finalize_and_report(rec, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
