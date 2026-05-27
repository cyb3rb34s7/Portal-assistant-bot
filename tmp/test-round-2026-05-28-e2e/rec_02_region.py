"""Scenario 2 native_select_region: select Region = Americas (AMER).
Record on draft A-9002. Replay with Asia Pacific (APAC).
Native <select>: set value + dispatch input/change so React onChange fires.
"""
import sys
from harness import CDP, BASE, console, connect_to_chrome, login_and_goto, make_recorder, pump, finalize_and_report

ASSET = "A-9002"


def set_native_select(page, sel, value):
    # React-safe native select: set .value then dispatch input+change.
    page.eval_on_selector(
        sel,
        """(el, v) => {
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value').set;
            setter.call(el, v);
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        value,
    )


def main():
    session = connect_to_chrome(CDP, target_url_substring=BASE)
    page, status = login_and_goto(session, ASSET)
    rec = make_recorder(session, "region_select")
    # Region select is populated from /api/regions (~800ms). Wait for it.
    page.wait_for_function(
        "() => document.querySelector('[data-testid=select-region]') && "
        "document.querySelector('[data-testid=select-region]').options.length > 1",
        timeout=10000,
    )
    pump(rec, page, 400)

    console.print("[cyan]selecting Region = Americas (AMER)[/cyan]")
    set_native_select(page, "[data-testid=select-region]", "AMER")
    pump(rec, page, 700)
    val = page.eval_on_selector("[data-testid=select-region]", "e => e.value")
    console.print(f"[magenta]region value now: {val}[/magenta]")

    finalize_and_report(rec, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
