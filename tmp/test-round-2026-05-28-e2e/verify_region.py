"""Verify the live Region select value via CDP after replay."""
import sys
sys.path.insert(0, "tmp/test-round-2026-05-28-e2e")
from harness import CDP, BASE, connect_to_chrome, console

s = connect_to_chrome(CDP, target_url_substring=BASE)
p = s.page
v = p.eval_on_selector("[data-testid=select-region]", "e => e.value")
txt = p.eval_on_selector("[data-testid=select-region]", "e => e.options[e.selectedIndex].textContent")
console.print(f"LIVE_REGION_VALUE={v} TEXT={txt}")
s.close()
