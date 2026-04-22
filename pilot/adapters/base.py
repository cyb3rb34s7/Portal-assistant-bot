"""Base adapter — enforces the MCP-ready interface rules.

Every adapter method must follow the four rules from the architecture doc:

  1. Primitive inputs only — no internal state objects passed as arguments.
  2. Returns a `ToolResult` — nothing else.
  3. Plain-English docstring describing intent, inputs, success condition.
  4. No undeclared side effects — a method that does X does not silently do Y.

The base class gives adapters access to the Playwright `page` and the
audit logger, but contains no portal-specific logic.
"""

from __future__ import annotations

from typing import Optional

from playwright.sync_api import Page

from ..audit import AuditLogger


class BaseAdapter:
    name: str = "base"

    def __init__(self, page: Page, audit: AuditLogger, base_url: str = ""):
        self.page = page
        self.audit = audit
        self.base_url = base_url.rstrip("/")

    def _screenshot(self, label: str) -> Optional[str]:
        return self.audit.screenshot(self.page, label) or None
