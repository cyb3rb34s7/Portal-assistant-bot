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
from collections import deque
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
        # Raw JSON payloads from the grabber, drained by the main loop.
        # The binding callback MUST NOT call Playwright methods (like
        # page.screenshot) because that reenters the sync_playwright
        # event loop and deadlocks. We just stash strings here and
        # process them later from the pumping loop.
        self._pending_payloads: deque[str] = deque()

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.shots_dir.mkdir(parents=True, exist_ok=True)

    # ---- Setup ---------------------------------------------------------

    def _inject(self) -> None:
        """Attach the grabber script + the Python-callback binding.

        Binding attachment is the single most failure-prone step in teach.
        We therefore:
          1. Expose the binding on the context.
          2. Add the grabber to `add_init_script` so every future page
             (including reloads) gets it.
          3. Reload the current page once so the pre-existing tab picks up
             *both* the init script and the binding on a fresh frame. This
             is the reliable way to attach to a CDP-connected Chrome tab
             that existed before we connected.
          4. Send a probe event from the page and confirm it round-trips
             to Python. If it doesn't, fail loudly instead of letting the
             operator record silently into the void.
        """
        script = OVERLAY_PATH.read_text(encoding="utf-8")
        context = self.session.context
        page = self.session.page

        self._probe_received = False

        # Expose Python callback as window.__pilotCapture.
        #
        # CRITICAL: this runs inside the Playwright event loop. It must
        # NOT call any Playwright method (no page.screenshot, no evaluate,
        # nothing) because sync_playwright reentrancy deadlocks. We only
        # enqueue the raw payload; the main pumping loop processes it.
        def on_capture(source, payload_json: str) -> None:
            try:
                if '"__init_probe"' in payload_json:
                    self._probe_received = True
                    return
            except Exception:
                pass
            self._pending_payloads.append(payload_json)

        # Playwright's expose_binding on the context applies to all pages
        # created after this call. A pre-existing page needs a reload to
        # receive the binding routing.
        try:
            context.expose_binding("__pilotCapture", on_capture)
        except Exception:
            # Already bound in this context (previous invocation in same Chrome)
            pass

        # Inject grabber into every future navigation + fresh frame load
        context.add_init_script(script)

        # Reload the current page so both the init script and the binding
        # attach cleanly. This is the fix for the "events captured client-
        # side but never reach Python" failure mode.
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception as e:
            self.console.print(
                f"[yellow]Could not reload page (continuing): {e}[/yellow]"
            )

        # Binding round-trip probe. If the binding is not wired up for
        # this page's JS context, this call will either raise or the
        # probe event will never arrive. Either way, we surface it.
        try:
            page.evaluate(
                "window.__pilotCapture && window.__pilotCapture("
                "JSON.stringify({kind: '__init_probe', page_url: location.href}))"
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to invoke __pilotCapture on the current page "
                f"({page.url}). The teach binding is not attached. "
                "Close all other Chrome tabs, ensure Chrome was launched "
                "with --remote-debugging-port=9222, and retry."
            ) from e

        # Pump the event loop briefly so the probe can round-trip.
        for _ in range(20):
            if self._probe_received:
                break
            try:
                page.wait_for_timeout(50)
            except Exception:
                time.sleep(0.05)

        if not self._probe_received:
            raise RuntimeError(
                "Teach binding probe sent but not received by Python. "
                "This usually means the Chrome tab was opened before the "
                "CDP connection was established. Restart Chrome with the "
                "portal URL and retry."
            )

    # ---- Event sink ----------------------------------------------------

    def _drain_pending(self) -> int:
        """Process any queued raw payloads. Called from the main loop,
        where it is safe to invoke Playwright methods (e.g. screenshots).

        Returns the number of events processed this call.
        """
        processed = 0
        while True:
            try:
                payload_json = self._pending_payloads.popleft()
            except IndexError:
                break
            self._process_payload(payload_json)
            processed += 1
        return processed

    def _process_payload(self, payload_json: str) -> None:
        try:
            raw = json.loads(payload_json)
        except Exception:
            return
        fp_raw = raw.get("fingerprint")
        try:
            fp = ElementFingerprint.model_validate(fp_raw) if fp_raw else None
        except Exception:
            fp = None

        ev = TraceEvent(
            ts=datetime.utcnow(),
            kind=raw.get("kind"),
            fingerprint=fp,
            value=raw.get("value"),
            url=raw.get("url"),
            file_name=raw.get("file_name"),
            page_url=raw.get("page_url", ""),
        )

        # Screenshot is safe here — we're on the main thread, outside
        # the Playwright binding callback.
        if self.capture_screenshots and ev.kind in ("click", "submit", "file_selected"):
            ev.screenshot_path = self._screenshot(
                f"{len(self._events):03d}_{ev.kind}"
            )

        with self._lock:
            self._events.append(ev)
            with self.trace_path.open("a", encoding="utf-8") as f:
                f.write(ev.model_dump_json() + "\n")

        self._render_live(ev)

    # Backwards-compatible alias in case external callers used _on_event.
    def _on_event(self, payload_json: str) -> None:
        self._process_payload(payload_json)

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
        last_heartbeat = time.time()
        last_event_count = 0
        try:
            # Pump the Playwright event loop actively so binding callbacks
            # enqueue payloads into self._pending_payloads, then drain that
            # queue on the main thread where Playwright calls (screenshots,
            # etc.) are safe. A plain time.sleep() starves the sync_playwright
            # loop; calling Playwright from the binding callback deadlocks
            # via sync-API reentrancy. This loop avoids both.
            while not self._stop.is_set():
                try:
                    self.session.page.wait_for_timeout(200)
                except Exception:
                    # Page may have been closed or navigated; sleep briefly
                    # and continue so the loop can still exit on Ctrl+C.
                    time.sleep(0.2)

                # Drain any events captured while the loop was waiting.
                try:
                    self._drain_pending()
                except Exception as e:
                    self.console.print(
                        f"[yellow]event drain error (continuing): {e}[/yellow]"
                    )

                now = time.time()
                if now - last_heartbeat >= 10.0:
                    with self._lock:
                        current = len(self._events)
                    if current == last_event_count:
                        self.console.print(
                            f"[dim]... {current} events captured so far "
                            f"(no new events in last 10s)[/dim]"
                        )
                    last_event_count = current
                    last_heartbeat = now
        finally:
            signal.signal(signal.SIGINT, prev)

        # Final drain in case anything queued between last tick and stop.
        try:
            self._drain_pending()
        except Exception:
            pass
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
