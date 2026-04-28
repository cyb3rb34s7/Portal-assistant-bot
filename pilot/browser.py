"""Browser connectivity helper.

Connects Playwright to the operator's existing Chrome session over CDP.
Never launches a separate headless browser. The operator is expected to
have started Chrome with --remote-debugging-port=9222 (see scripts/).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)


DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"
"""Use the IPv4 loopback address explicitly. On Windows, ``localhost``
resolves to ``::1`` first; Chrome's ``--remote-debugging-port`` binds
to ``127.0.0.1`` only, so a Playwright ``connect_over_cdp`` against
localhost gets ``ECONNREFUSED ::1:9222``. Hardcoding 127.0.0.1 is
correct on every OS and matches the actual address Chrome listens on."""


@dataclass
class BrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page

    def close(self) -> None:
        # We deliberately do NOT close the browser or the context —
        # that is the operator's Chrome. We only stop the Playwright driver.
        try:
            self.playwright.stop()
        except Exception:
            pass


def connect_to_chrome(
    cdp_endpoint: str = DEFAULT_CDP_ENDPOINT,
    target_url_substring: Optional[str] = None,
) -> BrowserSession:
    """Attach to the operator's Chrome via CDP and pick a working page.

    If target_url_substring is provided, prefer a page whose URL contains it.
    Otherwise, use the first non-devtools page. If no page exists, create one
    in the existing default context.
    """
    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(cdp_endpoint)

    if not browser.contexts:
        raise RuntimeError(
            "No browser contexts available. Ensure Chrome was launched with "
            "--remote-debugging-port=9222 and has at least one window open."
        )

    context = browser.contexts[0]
    page = _select_page(context, target_url_substring)
    return BrowserSession(
        playwright=playwright, browser=browser, context=context, page=page
    )


def _select_page(
    context: BrowserContext, target_url_substring: Optional[str]
) -> Page:
    pages = [p for p in context.pages if not p.url.startswith("devtools://")]
    if target_url_substring:
        for p in pages:
            if target_url_substring in p.url:
                return p
    if pages:
        return pages[0]
    return context.new_page()
