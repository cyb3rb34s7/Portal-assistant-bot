"""CurationPilot POC package."""

from __future__ import annotations

import sys

__version__ = "0.1.0"


# On Windows with a legacy console codepage, Rich's write path can hit
# UnicodeEncodeError when our output contains characters outside cp1252.
# Reconfigure the stdio streams to UTF-8 at import time so the toolkit
# behaves the same everywhere.
def _force_utf8_streams() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_streams()
