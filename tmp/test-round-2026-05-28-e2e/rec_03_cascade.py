"""Scenario 3 cascading: Region=APAC -> wait markets -> Market=India(IN)
-> wait languages -> Language=Hindi(hi-IN). Record on draft A-1001.
Replay with a different leaf (Market=Japan / Language=Japanese) if the
chain parameterizes; else same values to prove the cascade waits hold.
"""
import sys
from harness import CDP, BASE, console, connect_to_chrome, login_and_goto, make_recorder, pump, finalize_and_report

ASSET = "A-1001"


def set_native_select(page, sel, value):
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


def wait_select_has(page, sel, value, timeout=10000):
    page.wait_for_function(
        "([s,v]) => {const el=document.querySelector(s); return el && "
        "Array.from(el.options).some(o=>o.value===v);}",
        arg=[sel, value],
        timeout=timeout,
    )


def main():
    session = connect_to_chrome(CDP, target_url_substring=BASE)
    page, status = login_and_goto(session, ASSET)
    rec = make_recorder(session, "cascade_region_market_lang")
    wait_select_has(page, "[data-testid=select-region]", "APAC")
    pump(rec, page, 400)

    console.print("[cyan]Region = APAC[/cyan]")
    set_native_select(page, "[data-testid=select-region]", "APAC")
    pump(rec, page, 500)
    # Wait for the dependent Market list to repopulate (/api/markets ~2s).
    wait_select_has(page, "[data-testid=select-market]", "IN")
    console.print("[green]markets loaded (IN present)[/green]")
    pump(rec, page, 500)

    console.print("[cyan]Market = India (IN)[/cyan]")
    set_native_select(page, "[data-testid=select-market]", "IN")
    pump(rec, page, 500)
    # Wait for Language list (/api/languages ~1.5s).
    wait_select_has(page, "[data-testid=select-language]", "hi-IN")
    console.print("[green]languages loaded (hi-IN present)[/green]")
    pump(rec, page, 500)

    console.print("[cyan]Language = Hindi (hi-IN)[/cyan]")
    set_native_select(page, "[data-testid=select-language]", "hi-IN")
    pump(rec, page, 600)

    finalize_and_report(rec, session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
