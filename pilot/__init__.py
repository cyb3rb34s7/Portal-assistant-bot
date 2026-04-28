"""CurationPilot POC package."""

from __future__ import annotations

import os
import sys
from pathlib import Path

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


def _load_dotenv_if_available() -> None:
    """Load a repo-root .env file into os.environ if python-dotenv is
    installed.

    Looked up by walking parents of this file's directory until either
    a .env or pyproject.toml is found. Variables already set in the
    shell take precedence — ``override=False`` matches the standard
    expectation that explicit env vars win over .env defaults.

    Silent no-op if python-dotenv isn't installed; everything else
    works the same way it did before.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        env_path = candidate / ".env"
        if env_path.is_file():
            load_dotenv(dotenv_path=env_path, override=False)
            return
        if (candidate / "pyproject.toml").is_file():
            # Reached repo root with no .env — that's fine, just stop.
            return


_force_utf8_streams()
_load_dotenv_if_available()
