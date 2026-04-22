"""Teach mode — passive recorder.

Injects grabber.js into the operator's Chrome via CDP and receives
captured events on window.__pilotCapture, which we bind to a Python
callback. Each event is written to sessions/<id>/trace.jsonl as a raw
TraceEvent. Optional screenshots are taken after each interaction.

The recording stops when the operator hits Ctrl+C (or when an
external caller invokes stop()).
"""

from __future__ import annotations

import json
import signal
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .browser import BrowserSession, connect_to_chrome
from .skill_models import ElementFingerprint, TraceEvent


OVERLAY_PATH = Path(__file__).parent / "overlay" / "grabber.js"


class TeachRecorder:
    def __init__(
        self,
        session: BrowserSession,
        sessions_dir: Path,
        skill_name: str,
        console: Optional[Console] = None,
        capture_screenshots: bool = True,
    ):
        self.session = session
        self.sessions_dir = sessions_dir
        self.skill_name = skill_name
        self.session_id = uuid.uuid4().hex[:12]
        self.session_dir = sessions_dir / self.session_id
        self.shots_dir = self.session_dir / "screenshots"
        self.trace_path = self.session_dir / "trace.jsonl"
        self.meta_path = self.session_dir / "meta.json"
        self.console = console or Console()
        self.capture_screenshots = capture_screenshots

        self._stop = threading.Event()
        self._events: list[TraceEvent] = []
        self._lock = threading.Lock()

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.shots_dir.mkdir(parents=True, exist_ok=True)

    # ---- Setup ---------------------------------------------------------

    def _inject(self) -> None:
        """Attach the grabber script + the Python-callback binding."""
        script = OVERLAY_PATH.read_text(encoding="utf-8")
        context = self.session.context
        page = self.session.page

        # Expose Python callback as window.__pilotCapture
        def on_capture(source, payload_json: str) -> None:
            self._on_event(payload_json)

        # Playwright's expose_binding on the context applies to all pages
        try:
            context.expose_binding("__pilotCapture", on_capture)
        except Exception:
            # Already bound in this context (previous invocation in same Chrome)
            pass

        # Inject grabber into every future navigation
        context.add_init_script(script)

        # Inject immediately for the currently loaded page
        try:
            page.evaluate(script)
        except Exception as e:
            self.console.print(
                f"[yellow]Could not inject on current page (continuing): {e}[/yellow]"
            )

    # ---- Event sink ----------------------------------------------------

    def _on_event(self, payload_json: str) -> None:
        try:
            raw = json.loads(payload_json)
        except Exception:
            return
        fp_raw = raw.get("fingerprint")
        fp = ElementFingerprint.model_validate(fp_raw) if fp_raw else None

        ev = TraceEvent(
            ts=datetime.utcnow(),
            kind=raw.get("kind"),
            fingerprint=fp,
            value=raw.get("value"),
            url=raw.get("url"),
            file_name=raw.get("file_name"),
            page_url=raw.get("page_url", ""),
        )

        if self.capture_screenshots and ev.kind in ("click", "submit", "file_selected"):
            ev.screenshot_path = self._screenshot(f"{len(self._events):03d}_{ev.kind}")

        with self._lock:
            self._events.append(ev)
            with self.trace_path.open("a", encoding="utf-8") as f:
                f.write(ev.model_dump_json() + "\n")

        self._render_live(ev)

    def _screenshot(self, label: str) -> Optional[str]:
        try:
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
            path = self.shots_dir / f"{ts}_{label}.png"
            self.session.page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:
            return None

    def _render_live(self, ev: TraceEvent) -> None:
        fp = ev.fingerprint
        label = ""
        if fp:
            label = (
                fp.test_id
                or fp.accessible_name
                or fp.text
                or fp.element_id
                or fp.tag
                or ""
            )
            if label and len(label) > 60:
                label = label[:57] + "..."
        val = ev.value or ev.file_name or ev.url or ""
        if val and len(val) > 40:
            val = val[:37] + "..."
        self.console.print(
            f"  [dim]#{len(self._events):03d}[/dim] "
            f"[cyan]{ev.kind:14s}[/cyan] "
            f"[white]{label:40s}[/white] "
            f"[green]{val}[/green]"
        )

    # ---- Lifecycle -----------------------------------------------------

    def setup(self) -> None:
        """Inject the grabber and write meta.json — for programmatic use
        where the caller drives the session itself (e.g. an integration
        test). The blocking start() method calls this internally."""
        self._inject()
        self._write_meta()

    def start(self) -> None:
        self.setup()
        self.console.print(
            Panel.fit(
                f"[bold]Teaching skill:[/bold] {self.skill_name}\n"
                f"Session: {self.session_id}\n"
                f"Trace:   {self.trace_path}\n\n"
                "Use the portal normally. Press [bold]Ctrl+C[/bold] when done.",
                title="CurationPilot — Teach Mode",
                border_style="cyan",
            )
        )

        def _handler(sig, frame):
            self._stop.set()

        prev = signal.signal(signal.SIGINT, _handler)
        try:
            while not self._stop.is_set():
                time.sleep(0.25)
        finally:
            signal.signal(signal.SIGINT, prev)

        self._finalize()

    def stop(self) -> None:
        self._stop.set()

    def _write_meta(self) -> None:
        meta = {
            "session_id": self.session_id,
            "skill_name": self.skill_name,
            "started_at": datetime.utcnow().isoformat(),
            "base_url": "",
        }
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _finalize(self) -> None:
        meta: dict = {}
        if self.meta_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        if not meta:
            meta = {
                "session_id": self.session_id,
                "skill_name": self.skill_name,
                "started_at": datetime.utcnow().isoformat(),
                "base_url": "",
            }
        meta["ended_at"] = datetime.utcnow().isoformat()
        meta["event_count"] = len(self._events)
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        table = Table(title=f"Recorded {len(self._events)} events", header_style="bold")
        table.add_column("#", style="dim")
        table.add_column("Kind", style="cyan")
        table.add_column("Target")
        table.add_column("Value / URL")
        for i, ev in enumerate(self._events):
            fp = ev.fingerprint
            target = (fp.test_id or fp.accessible_name or fp.tag) if fp else ""
            val = ev.value or ev.file_name or ev.url or ""
            table.add_row(str(i), ev.kind, target or "", (val or "")[:40])
        self.console.print(table)
        self.console.print(
            f"\nTrace:  [bold]{self.trace_path}[/bold]"
            f"\nNext:   [bold]python -m pilot annotate {self.session_id}[/bold]"
        )


# ---- CLI entry point (used by pilot.cli) ----------------------------------


def run_teach(
    skill_name: str,
    base_url: str = "http://localhost:5173",
    cdp: str = "http://localhost:9222",
    sessions_dir: Path = Path("sessions"),
) -> str:
    """Connect to Chrome via CDP and record a teach session.

    Returns the session_id.
    """
    console = Console()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"Connecting to Chrome at [bold]{cdp}[/bold] ...")
    session = connect_to_chrome(cdp, target_url_substring=base_url)
    try:
        recorder = TeachRecorder(
            session=session,
            sessions_dir=sessions_dir,
            skill_name=skill_name,
            console=console,
        )
        recorder.start()
        return recorder.session_id
    finally:
        session.close()
