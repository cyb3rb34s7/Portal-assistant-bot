import sys
sys.path.insert(0, "tmp/test-round-2026-05-28-e2e")
from harness import CDP, BASE, connect_to_chrome, console
s = connect_to_chrome(CDP, target_url_substring=BASE)
p = s.page
for sel in ["select-region", "select-market", "select-language"]:
    v = p.eval_on_selector(f"[data-testid={sel}]", "e => e.value")
    t = p.eval_on_selector(f"[data-testid={sel}]", "e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].textContent : ''")
    console.print(f"{sel}={v} ({t})")
s.close()
