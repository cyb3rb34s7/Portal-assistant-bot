"""Scenario 4 country_multiselect_search: re-record the proven Country
server-search picker on draft A-1002 (record Argentina), then replay with
a DIFFERENT target (Brazil) to regression-guard the green case.
Mirrors tmp/layerb_record.py exactly.
"""
import sys
from harness import CDP, BASE, console, connect_to_chrome, login_and_goto, make_recorder, pump, finalize_and_report

ASSET = "A-1002"


def main():
    session = connect_to_chrome(CDP, target_url_substring=BASE)
    page, status = login_and_goto(session, ASSET)
    rec = make_recorder(session, "country_pick_r2")
    pump(rec, page, 400)

    console.print("[cyan]opening country picker[/cyan]")
    page.dispatch_event("[data-testid=multiselect-country-toggle]", "click")
    pump(rec, page, 700)

    # Surface several options first (populates known_options id+label).
    console.print("[cyan]searching 'a'[/cyan]")
    page.fill("[data-testid=multiselect-country-search]", "a")
    page.wait_for_selector("[data-testid^=multiselect-country-checkbox-]", timeout=8000)
    pump(rec, page, 700)

    console.print("[cyan]searching 'Argentina'[/cyan]")
    page.fill("[data-testid=multiselect-country-search]", "Argentina")
    page.wait_for_selector("[data-testid=multiselect-country-checkbox-ar]", timeout=8000)
    pump(rec, page, 600)

    console.print("[cyan]selecting Argentina[/cyan]")
    page.dispatch_event("[data-testid=multiselect-country-checkbox-ar]", "click")
    pump(rec, page, 500)

    page.dispatch_event("[data-testid=multiselect-country-toggle]", "click")
    pump(rec, page, 400)

    finalize_and_report(rec, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
