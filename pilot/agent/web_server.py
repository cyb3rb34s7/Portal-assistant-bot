"""FastAPI web server for the CurationPilot UI.

Wraps the existing Orchestrator + the new PortalLauncher behind a
small set of HTTP + WebSocket endpoints the React UI consumes. Same
business logic as the stdio JSON-RPC server (``pilot.agent.server``);
this is the second transport, not a fork.

Endpoints (mounted under /api):

  Portal browser:
    POST   /portal/launch         body: {target_url?}     -> PortalLauncherState
    GET    /portal/doctor                                  -> CDP probe + tab list
    POST   /portal/close                                    -> PortalLauncherState

  Teach:
    POST   /teach/start           body: {skill_name, portal_id?} -> {teach_id}
    POST   /teach/stop            body: {teach_id}           -> {session_id, event_count}
    POST   /teach/annotate        body: {session_id, name, llm: bool, client?} -> skill summary

  Skills:
    GET    /skills                                          -> list skills + sidecar info
    GET    /skills/<id>                                     -> one skill (raw JSON)

  Sessions:
    GET    /sessions                                        -> list past sessions
    GET    /sessions/<id>/report                            -> raw report.md
    GET    /sessions/<id>/screenshots/<file>                -> image

  Replay (replay flow uses Orchestrator):
    POST   /tasks                 multipart: goal + attachments + portal_id
    POST   /commands              body: HostCommand
    WS     /events?task_id=X                                -> NDJSON stream

Run with:
    .venv/Scripts/python.exe -m pilot serve

Or via uvicorn directly:
    .venv/Scripts/python.exe -m uvicorn pilot.agent.web_server:app --port 5177
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from pilot.agent.portal_launcher import PortalLauncher

# Lazy imports for the heavy parts so that an `import pilot.agent.web_server`
# in tests / type-checkers doesn't pull in Playwright, the agent, etc.


# ---------------------------------------------------------------------------
# App + global state
# ---------------------------------------------------------------------------


app = FastAPI(
    title="CurationPilot",
    version="0.2.0",
    description=(
        "Local web server for the CurationPilot UI. Wraps the same "
        "Orchestrator the stdio JSON-RPC server uses; both transports "
        "share business logic."
    ),
)

# In dev, the React app runs on Vite's port (5174) and proxies /api/*
# to here. CORS is permissive on localhost only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Single-process state. The web server is single-tenant by design —
# one operator, one portal browser, one task at a time.
_launcher = PortalLauncher()


@dataclass
class _TeachSession:
    teach_id: str
    skill_name: str
    portal_id: str | None
    base_url: str
    recorder: Any  # pilot.teach.TeachRecorder, lazily imported
    session: Any  # pilot.browser.BrowserSession
    pump_task: Any  # asyncio Task that pumps the recorder's pending payloads
    stop_event: threading.Event
    started_at: datetime
    finalized: bool = False


# Active teach session (one at a time).
_teach: _TeachSession | None = None


# ---------------------------------------------------------------------------
# Dirs
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    # pilot/agent/web_server.py -> repo root is two levels up.
    return Path(__file__).resolve().parents[2]


def _sessions_dir() -> Path:
    return _repo_root() / "sessions"


def _skills_dir() -> Path:
    return _repo_root() / "skills"


def _portals_dir() -> Path:
    return _repo_root() / "portals"


# ---------------------------------------------------------------------------
# Portal browser endpoints
# ---------------------------------------------------------------------------


@app.post("/api/portal/launch")
async def portal_launch(payload: dict | None = None) -> dict:
    target_url = (payload or {}).get("target_url")
    try:
        state = _launcher.launch(target_url=target_url)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return _state_to_dict(state)


@app.get("/api/portal/doctor")
async def portal_doctor() -> dict:
    # Doctor doesn't require a launched-by-us browser; works against
    # any Chrome listening on the CDP port (e.g. the operator may
    # have launched it manually).
    return _launcher.doctor()


@app.post("/api/portal/close")
async def portal_close() -> dict:
    state = _launcher.close()
    return _state_to_dict(state)


def _state_to_dict(state) -> dict:
    return {
        "status": state.status,
        "pid": state.pid,
        "cdp_url": state.cdp_url,
        "target_url": state.target_url,
        "profile_dir": state.profile_dir,
        "started_at": state.started_at,
        "last_error": state.last_error,
    }


# ---------------------------------------------------------------------------
# Teach endpoints
# ---------------------------------------------------------------------------


@app.post("/api/teach/start")
async def teach_start(payload: dict) -> dict:
    """Begin a teach session against the running portal Chrome.

    Idempotent only in the trivial sense — calling start twice is an
    error; the operator must stop one before starting another.
    """
    global _teach
    if _teach is not None and not _teach.finalized:
        raise HTTPException(
            status_code=409,
            detail="another teach session is already running; stop it first",
        )

    skill_name = payload.get("skill_name")
    if not skill_name or not isinstance(skill_name, str):
        raise HTTPException(status_code=400, detail="skill_name required")
    portal_id = payload.get("portal_id")
    base_url = payload.get("base_url") or "http://localhost:5173"

    # Lazy import — avoids loading Playwright on server start when no
    # one is teaching yet.
    from pilot.browser import connect_to_chrome
    from pilot.teach import TeachRecorder

    try:
        session = connect_to_chrome(
            cdp_endpoint=_launcher.state.cdp_url,
            target_url_substring=base_url,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"could not attach to Chrome at {_launcher.state.cdp_url}: {e}",
        )

    recorder = TeachRecorder(
        session=session,
        sessions_dir=_sessions_dir(),
        skill_name=skill_name,
        portal_id=portal_id,
        portals_dir=_portals_dir(),
        capture_screenshots=True,
    )
    try:
        recorder.setup()
    except Exception as e:  # noqa: BLE001
        try:
            session.close()
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"teach setup failed: {e}")

    teach_id = uuid.uuid4().hex[:8]
    stop_event = threading.Event()

    # Background pump: drain pending payloads + push live event count
    # to any subscribers (handled via the event broker below).
    pump_task = asyncio.create_task(
        _teach_pump(recorder, stop_event, teach_id)
    )

    _teach = _TeachSession(
        teach_id=teach_id,
        skill_name=skill_name,
        portal_id=portal_id,
        base_url=base_url,
        recorder=recorder,
        session=session,
        pump_task=pump_task,
        stop_event=stop_event,
        started_at=datetime.utcnow(),
    )

    return {
        "teach_id": teach_id,
        "skill_name": skill_name,
        "session_id": recorder.session_id,
        "started_at": _teach.started_at.isoformat(),
    }


@app.post("/api/teach/stop")
async def teach_stop(payload: dict) -> dict:
    """Finalize the active teach session — flush events, write meta."""
    global _teach
    if _teach is None or _teach.finalized:
        raise HTTPException(
            status_code=409, detail="no active teach session"
        )
    teach_id = payload.get("teach_id")
    if teach_id and teach_id != _teach.teach_id:
        raise HTTPException(
            status_code=409, detail="teach_id does not match active session"
        )

    # Tell pump to stop, wait for it, then finalize.
    _teach.stop_event.set()
    try:
        await asyncio.wait_for(_teach.pump_task, timeout=5)
    except (asyncio.TimeoutError, Exception):
        pass

    # Final drain + finalize on a worker so we don't block the loop.
    def _finalize_in_worker():
        try:
            _teach.recorder._drain_pending()
        except Exception:
            pass
        try:
            _teach.recorder._finalize()
        except Exception:
            pass
        try:
            _teach.session.close()
        except Exception:
            pass

    await asyncio.to_thread(_finalize_in_worker)

    _teach.finalized = True
    out = {
        "teach_id": _teach.teach_id,
        "session_id": _teach.recorder.session_id,
        "event_count": len(getattr(_teach.recorder, "_events", [])),
        "skill_name": _teach.skill_name,
    }
    _teach = None
    return out


@app.post("/api/teach/annotate")
async def teach_annotate(payload: dict) -> dict:
    """Run annotate (auto) and optionally annotate-LLM on a finalized
    teach session. Returns a summary the UI can render."""
    session_id = payload.get("session_id")
    skill_name = payload.get("name") or session_id
    use_llm = bool(payload.get("llm", False))
    client_name = payload.get("client") or os.environ.get(
        "CURATIONPILOT_AI_CLIENT", "groq"
    )
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    session_dir = _sessions_dir() / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="session not found")

    from pilot.annotate import run_annotate

    skill_path = await asyncio.to_thread(
        run_annotate,
        session_id=session_id,
        skill_name=skill_name,
        auto=True,
        sessions_dir=_sessions_dir(),
        skills_dir=_skills_dir(),
    )

    sidecar_path: Path | None = None
    if use_llm:
        try:
            from pilot.agent.ai_client import get_client
            from pilot.agent.annotate_llm import annotate_skill, write_v2_sidecar

            client = get_client(client_name)
            v1_skill = json.loads(skill_path.read_text(encoding="utf-8"))
            v2_meta = await annotate_skill(
                client=client, v1_skill=v1_skill
            )
            sidecar_path = write_v2_sidecar(skill_path, v2_meta)
            await client.close()
        except Exception as e:  # noqa: BLE001
            return {
                "skill_name": skill_name,
                "skill_path": str(skill_path),
                "sidecar_path": None,
                "annotate_llm_error": f"{type(e).__name__}: {e}",
            }

    skill_data = json.loads(skill_path.read_text(encoding="utf-8"))
    sidecar_data = (
        json.loads(sidecar_path.read_text(encoding="utf-8"))
        if sidecar_path and sidecar_path.exists()
        else None
    )
    return {
        "skill_name": skill_name,
        "skill_path": str(skill_path),
        "sidecar_path": str(sidecar_path) if sidecar_path else None,
        "v1_param_count": len(skill_data.get("params", [])),
        "step_count": len(skill_data.get("steps", [])),
        "v2_parameters": (sidecar_data or {}).get("parameters", []),
        "v2_alias_map": (sidecar_data or {}).get("param_alias_map", {}),
        "v2_destructive_actions": (sidecar_data or {}).get(
            "destructive_actions", []
        ),
    }


# ---------------------------------------------------------------------------
# Skills + sessions endpoints
# ---------------------------------------------------------------------------


@app.get("/api/skills")
async def list_skills() -> list[dict]:
    out: list[dict] = []
    for p in sorted(_skills_dir().glob("*.json")):
        if p.name.endswith(".v2.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        sidecar = p.with_suffix(".v2.json")
        side = None
        if sidecar.exists():
            try:
                side = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                side = None
        out.append(
            {
                "id": data.get("name") or p.stem,
                "name": data.get("name") or p.stem,
                "description": (side or {}).get("description")
                or data.get("description"),
                "step_count": len(data.get("steps", [])),
                "param_count": len(
                    (side or {}).get("parameters") or data.get("params", [])
                ),
                "destructive_action_count": len(
                    (side or {}).get("destructive_actions") or []
                ),
                "has_sidecar": sidecar.exists(),
                "path": str(p.relative_to(_repo_root())),
                "updated_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            }
        )
    return out


@app.get("/api/skills/{skill_id}")
async def get_skill(skill_id: str) -> dict:
    p = _skills_dir() / f"{skill_id}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="skill not found")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/sessions")
async def list_sessions() -> list[dict]:
    sessions_dir = _sessions_dir()
    if not sessions_dir.exists():
        return []
    out: list[dict] = []
    for d in sorted(
        sessions_dir.iterdir(),
        key=lambda x: x.stat().st_mtime if x.exists() else 0,
        reverse=True,
    ):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        report_path = d / "report.md"
        out.append(
            {
                "id": d.name,
                "skill_name": meta.get("skill_name"),
                "started_at": meta.get("started_at"),
                "ended_at": meta.get("ended_at"),
                "event_count": meta.get("event_count"),
                "has_report": report_path.exists(),
                "screenshot_count": (
                    len(list((d / "screenshots").glob("*")))
                    if (d / "screenshots").exists()
                    else 0
                ),
            }
        )
    return out


@app.get("/api/sessions/{session_id}/report")
async def get_session_report(session_id: str) -> PlainTextResponse:
    p = _sessions_dir() / session_id / "report.md"
    if not p.exists():
        raise HTTPException(status_code=404, detail="report not found")
    return PlainTextResponse(
        p.read_text(encoding="utf-8"), media_type="text/markdown"
    )


@app.get("/api/sessions/{session_id}/screenshots/{filename}")
async def get_session_screenshot(session_id: str, filename: str) -> FileResponse:
    # Defensive: filename must not escape the screenshots dir.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    p = _sessions_dir() / session_id / "screenshots" / filename
    if not p.exists():
        raise HTTPException(status_code=404, detail="screenshot not found")
    return FileResponse(p)


# ---------------------------------------------------------------------------
# Event bus + WebSocket
# ---------------------------------------------------------------------------


class _EventBus:
    """Per-process pub/sub for live events. Subscribers receive every
    event published after they subscribed. Bounded queues prevent a
    slow client from leaking memory."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def publish(self, payload: dict) -> None:
        line = json.dumps(payload, default=str)
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                # Drop on the floor for slow consumers.
                pass


_bus = _EventBus()


@app.websocket("/api/events")
async def ws_events(ws: WebSocket) -> None:
    await ws.accept()
    q = await _bus.subscribe()
    try:
        # Send a welcome / capability event so the UI knows we're alive.
        await ws.send_text(
            json.dumps(
                {
                    "type": "agent.ready",
                    "v": 1,
                    "ts": datetime.utcnow().isoformat() + "Z",
                    "agent_version": "0.2.0",
                    "capabilities": {"transport": "ws"},
                }
            )
        )
        while True:
            line = await q.get()
            await ws.send_text(line)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await _bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# Teach pump — drains the recorder's event queue + republishes to the bus
# ---------------------------------------------------------------------------


async def _teach_pump(
    recorder, stop_event: threading.Event, teach_id: str
) -> None:
    """Background drain. Calls recorder._drain_pending() periodically
    and publishes a count update so the UI can show "captured N
    events" in real time. Polls the page (via Playwright) to keep the
    sync_playwright event loop moving — without this the binding
    callbacks never fire."""

    last_count = -1
    while not stop_event.is_set():
        # Drain on a worker thread — _drain_pending may write to disk
        # and call Playwright (screenshots). Both must happen off the
        # asyncio loop to avoid blocking concurrent requests.
        try:
            await asyncio.to_thread(_pump_tick, recorder)
        except Exception:
            pass

        n = len(getattr(recorder, "_events", []))
        if n != last_count:
            await _bus.publish(
                {
                    "type": "teach.event_count",
                    "teach_id": teach_id,
                    "count": n,
                    "ts": datetime.utcnow().isoformat() + "Z",
                }
            )
            last_count = n
        await asyncio.sleep(0.25)


def _pump_tick(recorder) -> None:
    """One drain tick. Pumps Playwright via a tiny wait_for_timeout so
    binding callbacks fire, then drains pending payloads."""
    try:
        recorder.session.page.wait_for_timeout(50)
    except Exception:
        pass
    try:
        recorder._drain_pending()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI entry — `pilot serve`
# ---------------------------------------------------------------------------


def serve(host: str = "127.0.0.1", port: int = 5177) -> None:
    """Start the FastAPI server on a loopback interface."""
    import uvicorn

    print(f"\nCurationPilot UI server\n  http://{host}:{port}\n")
    uvicorn.run(
        "pilot.agent.web_server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
