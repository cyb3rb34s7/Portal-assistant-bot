"""WI-49: canvas / SVG / media gesture adapters.

A canvas gesture adapter takes a STRUCTURED intent
(``{kind: 'draw_box', coords: [x, y, w, h]}``) and dispatches the
Playwright pointer events that realize that intent on the specific
canvas library the portal uses. The runner looks up the adapter by
name in the registry below; unknown name -> the runner emits
``error_kind='action_not_implemented'`` so the operator sees exactly
which adapter to register.

This module ships ONE built-in adapter, ``noop_click``, which
dispatches a single click at the center of the target descriptor's
selector. It exists so the schema + dispatch path is tested
end-to-end (per the WI-49 acceptance check) without requiring a real
canvas portal with library-specific quirks.

Third-party adapters subclass ``CanvasAdapter`` and call
``register_canvas_adapter('name', YourAdapter)`` at import time. The
adapter contract is intentionally narrow:

  - ``dispatch(target_descriptor: dict, action_payload: dict) -> ToolResult``

so the runner can hand structured data through and receive a uniform
result. Adapters are constructed per-step with ``(page, audit)``; do
not stash state across calls.
"""

from __future__ import annotations

from typing import Any, Optional, Type

from playwright.sync_api import Page

from ..audit import AuditLogger
from ..models import ToolResult


class CanvasAdapter:
    """Base class for WI-49 canvas gesture adapters.

    Subclasses MUST implement ``dispatch``. ``page`` and ``audit`` are
    the per-step injection points -- the runner builds a fresh adapter
    instance for each canvas_gesture step so no stash-state across
    calls is possible.
    """

    name: str = "base"

    def __init__(self, page: Page, audit: AuditLogger):
        self.page = page
        self.audit = audit

    def dispatch(
        self,
        target_descriptor: dict[str, Any],
        action_payload: dict[str, Any],
    ) -> ToolResult:
        raise NotImplementedError(
            "CanvasAdapter subclass must implement dispatch()"
        )


# ----- noop_click -----------------------------------------------------------


class CanvasNoopClickAdapter(CanvasAdapter):
    """WI-49 sample adapter: dispatches a click at the center of the
    target descriptor's selector.

    Exists for two reasons:

      1. The acceptance check requires a working canvas_gesture step.
         Until a real portal-specific adapter lands, noop_click proves
         the schema + runner dispatch + ToolResult shape work
         end-to-end.

      2. Operators integrating a new canvas portal can use noop_click
         as a smoke test before authoring the real adapter.

    Target descriptor: ``{selector: '<css>'}`` (CSS selector).
    Action payload (optional): ``{kind: 'click_center'}`` -- the only
    supported kind today; unknown kinds are accepted (audit-only) and
    the adapter still clicks the center.
    """

    name = "noop_click"

    def dispatch(
        self,
        target_descriptor: dict[str, Any],
        action_payload: dict[str, Any],
    ) -> ToolResult:
        selector = target_descriptor.get("selector")
        if not selector:
            return ToolResult(
                success=False,
                action_taken="noop_click canvas dispatch",
                error=(
                    "noop_click adapter requires "
                    "target_descriptor.selector"
                ),
                error_kind="canvas_gesture_bad_descriptor",
            )
        try:
            locator = self.page.locator(selector).first
            locator.click(timeout=4000)
        except Exception as e:
            return ToolResult(
                success=False,
                action_taken="noop_click canvas dispatch",
                error=str(e),
                error_kind="canvas_gesture_dispatch_failed",
                error_details={"selector": selector},
            )
        return ToolResult(
            success=True,
            action_taken=f"noop_click on {selector}",
            output={
                "selector": selector,
                "payload_kind": action_payload.get("kind", "click_center"),
            },
        )


# ----- registry -------------------------------------------------------------

_CANVAS_REGISTRY: dict[str, Type[CanvasAdapter]] = {}


def register_canvas_adapter(
    name: str, cls: Type[CanvasAdapter]
) -> None:
    """Register a canvas adapter under ``name``. Idempotent --
    registering the same name twice is a silent overwrite (the most
    recent registration wins). Names are case-sensitive."""
    _CANVAS_REGISTRY[name] = cls


def get_canvas_adapter(name: str) -> Optional[Type[CanvasAdapter]]:
    """Look up an adapter class by name. Returns None when unknown so
    the runner can emit a structured 'action_not_implemented' rather
    than raise KeyError."""
    return _CANVAS_REGISTRY.get(name)


# Built-in: noop_click. Registered eagerly at import time so the WI-49
# acceptance check (a step with adapter_name='noop_click' replays
# without any extra wiring) passes out of the box.
register_canvas_adapter("noop_click", CanvasNoopClickAdapter)
