"""Verify multiselect chips for a given testId prefix. Usage:
   python verify_chips.py multiselect-country
"""
import sys
sys.path.insert(0, "tmp/test-round-2026-05-28-e2e")
from harness import CDP, BASE, connect_to_chrome, console
prefix = sys.argv[1] if len(sys.argv) > 1 else "multiselect-country"
s = connect_to_chrome(CDP, target_url_substring=BASE)
p = s.page
chips = p.evaluate(
    "(pfx) => Array.from(document.querySelectorAll("
    "`[data-testid^='${pfx}-chip-']:not([data-testid$='-remove'])`))"
    ".map(e => e.textContent.replace('\\u00d7','').trim())",
    prefix,
)
console.print(f"CHIPS({prefix})={chips}")
s.close()
