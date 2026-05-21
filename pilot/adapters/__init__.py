"""Portal adapters + WI-49 canvas-gesture adapters.

The legacy portal-adapter set (BaseAdapter, MediaAssetsAdapter) provides
deterministic write/read methods for entire portal pages -- those are
MCP-ready and live in this package since the project's first POC.

WI-49 adds a SEPARATE registry for canvas / SVG / media adapters that
realize structured intents (``{kind: 'draw_box', coords: [...]}``) as
pointer events on a specific canvas library. Canvas gestures live next
to portal adapters because both are per-portal extension points; we
keep them in distinct registries so:

  - registering a canvas adapter doesn't pollute portal-wide
    read/write APIs, and
  - a portal can ship a canvas adapter WITHOUT having a portal-wide
    BaseAdapter subclass at all.

The canvas registry is populated at import time. Built-in: ``noop_click``
(see CanvasNoopClickAdapter) -- it dispatches a click at the center of
the target descriptor's selector so WI-49's schema + dispatch path can
be tested end-to-end without a real canvas portal. Third-party portals
register via ``register_canvas_adapter(name, cls)``.
"""

from __future__ import annotations

from typing import Optional, Type

from .base import BaseAdapter
from .canvas import (
    CanvasAdapter,
    CanvasNoopClickAdapter,
    get_canvas_adapter,
    register_canvas_adapter,
)
from .media_assets import MediaAssetsAdapter

__all__ = [
    "BaseAdapter",
    "MediaAssetsAdapter",
    # WI-49 canvas adapter surface:
    "CanvasAdapter",
    "CanvasNoopClickAdapter",
    "get_canvas_adapter",
    "register_canvas_adapter",
]
