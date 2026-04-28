"""Reset the sample portal's in-memory state via the running CDP connection.

Clears localStorage (which is where the portal persists contents +
applied-layout history) and navigates back to /upload. Faster than
DevTools console + paste. Requires Chrome already running with
--remote-debugging-port=9222 (i.e. ``pilot doctor`` works).

Usage:
    py scripts/reset_portal.py
"""

from __future__ import annotations

import sys

from pilot.browser import connect_to_chrome


PORTAL_BASE = "http://localhost:5173"


def main() -> int:
    try:
        session = connect_to_chrome(target_url_substring=PORTAL_BASE)
    except Exception as e:
        print(f"could not attach to Chrome: {e}", file=sys.stderr)
        return 2

    try:
        page = session.page
        page.evaluate("() => { try { localStorage.clear(); } catch (e) {} }")
        page.goto(PORTAL_BASE + "/upload", wait_until="domcontentloaded")
        print("portal state cleared. now on /upload.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
