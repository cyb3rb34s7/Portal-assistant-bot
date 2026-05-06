"""Portal browser launcher.

Spawns a dedicated Chrome instance with CDP debugging enabled, against
an isolated user-data-dir so it never collides with the operator's
daily browser profile. The launched browser is what the operator
authenticates in (Keycloak / SSO / 2FA) and what the runner attaches
to via Playwright's ``connect_over_cdp``.

State model:
  IDLE -> LAUNCHING -> RUNNING -> CLOSING -> IDLE

Only one launched portal browser at a time per server process. A
second launch request when one is already running just returns the
running PID + URL.

Cross-platform: best-effort Chrome path detection on Windows, macOS,
and Linux. Falls back to a ``CHROME_PATH`` env var.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.request import urlopen


@dataclass
class PortalLauncherState:
    status: Literal["idle", "launching", "running", "closing"] = "idle"
    pid: int | None = None
    cdp_url: str = "http://127.0.0.1:9222"
    target_url: str | None = None
    profile_dir: str | None = None
    started_at: float | None = None
    last_error: str | None = None


_DEFAULT_PROFILE_NAME = ".curationpilot-portal-profile"
_DEFAULT_CDP_PORT = 9222


def _find_chrome_binary() -> str | None:
    """Locate the Chrome / Chromium executable. CHROME_PATH env wins
    if set; otherwise probes known install locations per OS."""
    explicit = os.environ.get("CHROME_PATH")
    if explicit and Path(explicit).exists():
        return explicit

    sys = platform.system().lower()
    candidates: list[str] = []

    if sys == "windows":
        program_files = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        ]
        for pf in program_files:
            if not pf:
                continue
            candidates.extend(
                [
                    str(Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                    str(Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                ]
            )
    elif sys == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
    else:
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
        ):
            found = shutil.which(name)
            if found:
                return found

    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _default_profile_dir() -> str:
    home = (
        os.environ.get("USERPROFILE")
        or os.environ.get("HOME")
        or str(Path.home())
    )
    return str(Path(home) / _DEFAULT_PROFILE_NAME)


def _wait_for_cdp(cdp_url: str, timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urlopen(f"{cdp_url}/json/version", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


class PortalLauncher:
    """Owns the lifecycle of a single dedicated Chrome instance.

    Stateful — instantiate once at server startup, keep around. The
    web server's launch/close endpoints call methods on this instance.
    """

    def __init__(
        self,
        cdp_port: int = _DEFAULT_CDP_PORT,
        profile_dir: str | None = None,
    ) -> None:
        self.cdp_port = cdp_port
        self.profile_dir = profile_dir or _default_profile_dir()
        self.state = PortalLauncherState(
            cdp_url=f"http://127.0.0.1:{cdp_port}",
            profile_dir=self.profile_dir,
        )
        self._proc: subprocess.Popen | None = None

    def launch(self, target_url: str | None = None) -> PortalLauncherState:
        """Launch Chrome with CDP, navigated to target_url (or
        about:blank). Idempotent. Raises RuntimeError on failure."""
        if self.is_running():
            self.state.target_url = target_url or self.state.target_url
            return self.state

        chrome = _find_chrome_binary()
        if not chrome:
            self.state.status = "idle"
            self.state.last_error = (
                "Chrome / Chromium not found. Set CHROME_PATH env var, "
                "or install Chrome."
            )
            raise RuntimeError(self.state.last_error)

        try:
            Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            self.state.last_error = f"could not create profile dir: {e}"
            raise RuntimeError(self.state.last_error) from e

        args = [
            chrome,
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            target_url or "about:blank",
        ]

        self.state.status = "launching"
        self.state.last_error = None
        self.state.target_url = target_url

        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system().lower() == "windows":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            )

        try:
            self._proc = subprocess.Popen(args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.state.status = "idle"
            self.state.last_error = f"failed to spawn Chrome: {e}"
            raise RuntimeError(self.state.last_error) from e

        if not _wait_for_cdp(self.state.cdp_url, timeout_s=15.0):
            pid = self._proc.pid if self._proc else "?"
            self._kill_proc()
            self.state.status = "idle"
            self.state.last_error = (
                f"Chrome process started (PID {pid}) but CDP did not "
                f"come up at {self.state.cdp_url}. Most likely cause: "
                "another Chrome instance is already using this profile "
                "dir (close all Chrome windows for this profile and "
                "retry), or the port is already in use."
            )
            raise RuntimeError(self.state.last_error)

        self.state.status = "running"
        self.state.pid = self._proc.pid if self._proc else None
        self.state.started_at = time.time()
        return self.state

    def close(self) -> PortalLauncherState:
        """Kill the launched Chrome. Idempotent."""
        if not self.is_running():
            self.state.status = "idle"
            self.state.pid = None
            return self.state
        self.state.status = "closing"
        self._kill_proc()
        self.state.status = "idle"
        self.state.pid = None
        return self.state

    def doctor(self) -> dict:
        """Probe the CDP endpoint. Returns a dict with `connected`,
        `tabs` (list of {url, title}), `error` (str | None), and
        `browser_version`. Works against any Chrome listening on the
        CDP port — including one started outside this process."""
        out: dict = {
            "connected": False,
            "cdp_url": self.state.cdp_url,
            "tabs": [],
            "error": None,
            "browser_version": None,
        }
        try:
            with urlopen(f"{self.state.cdp_url}/json/version", timeout=2) as r:
                import json as _json

                v = _json.loads(r.read().decode("utf-8", errors="replace"))
                out["browser_version"] = v.get("Browser", "")
        except Exception as e:  # noqa: BLE001
            out["error"] = f"CDP /json/version unreachable: {e}"
            return out

        try:
            with urlopen(f"{self.state.cdp_url}/json", timeout=2) as r:
                import json as _json

                tabs_raw = _json.loads(r.read().decode("utf-8", errors="replace"))
            out["tabs"] = [
                {
                    "id": t.get("id"),
                    "url": t.get("url"),
                    "title": t.get("title"),
                    "type": t.get("type"),
                }
                for t in tabs_raw
                if t.get("type") == "page"
            ]
            out["connected"] = True
        except Exception as e:  # noqa: BLE001
            out["error"] = f"CDP /json unreachable: {e}"
        return out

    def is_running(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def _kill_proc(self) -> None:
        if self._proc is None:
            return
        try:
            if platform.system().lower() == "windows":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except Exception:
            pass
        self._proc = None
