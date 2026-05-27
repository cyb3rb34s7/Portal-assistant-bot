"""Scenario 5 categories_multiselect: client-side multi-select (no server
search). Record: open Categories, check Sports + Drama, close. Replay with
a DIFFERENT set (Kids). Tests set_selection without server search +
label-aware equality. Recorded on draft B-2001.
"""
import sys
from harness import CDP, BASE, console, connect_to_chrome, login_and_goto, make_recorder, pump, finalize_and_report

ASSET = "B-2001"


def main():
    session = connect_to_chrome(CDP, target_url_substring=BASE)
    page, status = login_and_goto(session, ASSET)
    rec = make_recorder(session, "categories_pick")
    # Categories list from /api/categories (~700ms).
    page.wait_for_timeout(1200)
    pump(rec, page, 400)

    console.print("[cyan]opening categories picker[/cyan]")
    page.dispatch_event("[data-testid=multiselect-categories-toggle]", "click")
    pump(rec, page, 500)
    page.wait_for_selector("[data-testid=multiselect-categories-checkbox-sports]", timeout=8000)

    console.print("[cyan]checking Sports[/cyan]")
    page.dispatch_event("[data-testid=multiselect-categories-checkbox-sports]", "click")
    pump(rec, page, 400)

    console.print("[cyan]checking Drama[/cyan]")
    page.dispatch_event("[data-testid=multiselect-categories-checkbox-drama]", "click")
    pump(rec, page, 400)

    console.print("[cyan]closing picker[/cyan]")
    page.dispatch_event("[data-testid=multiselect-categories-toggle]", "click")
    pump(rec, page, 400)

    finalize_and_report(rec, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
