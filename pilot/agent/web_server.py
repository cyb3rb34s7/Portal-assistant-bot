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
    pool: Any  # concurrent.futures.ThreadPoolExecutor (max_workers=1)
    """Dedicated single-thread executor. sync_playwright is thread-
    affine — the BrowserSession created during setup() must be used
    only from the same OS thread, including pump ticks and finalize.
    Without this, every Playwright call from the asyncio loop trips
    'sync API inside asyncio loop' errors."""
    finalized: bool = False


# Active teach session (one at a time).
_teach: _TeachSession | None = None


@dataclass
class _ActiveTask:
    """Bookkeeping for a single in-flight Orchestrator run.

    The replay flow is single-tenant: one operator, one task at a time.
    Submitting a new task while one is running returns 409. Cancellation
    is the supported way to free the slot.
    """

    task_id: str
    orchestrator: Any  # pilot.agent.orchestrator.Orchestrator
    cmd_q: Any  # asyncio.Queue[HostCommand]
    ev_q: Any  # asyncio.Queue[AgentEvent]
    pump_task: Any  # asyncio.Task -- ev_q -> _bus
    runner_task: Any  # asyncio.Task -- orchestrator.run_task
    client: Any  # AIClient (we await client.close() on cleanup)
    started_at: datetime
    portal_id: str | None
    finished: bool = False


_active_task: _ActiveTask | None = None
_active_task_lock = asyncio.Lock()


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


# ---------------------------------------------------------------------------
# Portals (per-portal config files)
# ---------------------------------------------------------------------------


def _read_portal_context(portal_id: str) -> dict | None:
    """Return the parsed context.yaml for a portal id, or None."""
    path = _portals_dir() / portal_id / "context.yaml"
    if not path.exists():
        return None
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _portal_base_url(portal_id: str) -> str | None:
    """Look up base_url from a portal's context.yaml, or None."""
    ctx = _read_portal_context(portal_id)
    if not ctx:
        return None
    return ctx.get("base_url")


@app.get("/api/portals")
async def list_portals() -> list[dict]:
    """List portals declared under ``portals/<id>/context.yaml``.

    Returns one entry per portal with id, name, base_url, and a count
    of pages from the page_map. The UI uses this to populate a portal
    selector so operators don't have to type URLs (or remember them).
    """
    out: list[dict] = []
    pdir = _portals_dir()
    if not pdir.exists():
        return out
    for d in sorted(pdir.iterdir()):
        if not d.is_dir():
            continue
        ctx = _read_portal_context(d.name)
        if not ctx:
            continue
        out.append(
            {
                "id": ctx.get("portal_id") or d.name,
                "name": ctx.get("name") or d.name,
                "base_url": ctx.get("base_url") or "",
                "page_count": len(ctx.get("page_map") or []),
                "config_path": str((d / "context.yaml").relative_to(_repo_root())),
            }
        )
    return out


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

    Either ``portal_id`` (URL resolved from
    ``portals/<id>/context.yaml::base_url``) OR ``base_url`` (explicit
    override) is required. There is intentionally no default —
    CurationPilot drives any portal; baking a URL into the agent
    couples it to one.

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
    base_url = payload.get("base_url")
    if not base_url and portal_id:
        base_url = _portal_base_url(portal_id)
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail=(
                "either portal_id (with a portals/<id>/context.yaml that "
                "declares base_url) or an explicit base_url is required"
            ),
        )

    # Lazy import — avoids loading Playwright on server start when no
    # one is teaching yet.
    import concurrent.futures
    from pilot.browser import connect_to_chrome
    from pilot.teach import TeachRecorder

    # All sync_playwright work for this teach session must happen on a
    # SINGLE OS thread — sync_playwright is thread-affine. Calling
    # connect_to_chrome from the asyncio event-loop thread trips
    # 'Sync API inside the asyncio loop'. Calling subsequent Playwright
    # methods from a different worker thread than the one that created
    # the session also fails. So we dedicate one thread for the whole
    # teach lifetime: setup runs there, pump ticks run there, finalize
    # runs there.
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="teach-recorder"
    )
    loop = asyncio.get_running_loop()

    def _setup_in_worker() -> tuple[Any, Any]:
        session = connect_to_chrome(
            cdp_endpoint=_launcher.state.cdp_url,
            target_url_substring=base_url,
        )
        recorder = TeachRecorder(
            session=session,
            sessions_dir=_sessions_dir(),
            skill_name=skill_name,
            portal_id=portal_id,
            portals_dir=_portals_dir(),
            capture_screenshots=True,
        )
        recorder.setup()
        return session, recorder

    try:
        session, recorder = await loop.run_in_executor(pool, _setup_in_worker)
    except Exception as e:  # noqa: BLE001
        pool.shutdown(wait=False)
        # connect_to_chrome and recorder.setup() can both fail; surface
        # the actual error rather than swallowing it. 502 means "Chrome
        # not reachable" / "binding probe failed" — both are real
        # operator-actionable conditions.
        raise HTTPException(
            status_code=502,
            detail=(
                f"could not attach to Chrome at {_launcher.state.cdp_url} "
                f"or set up teach binding: {type(e).__name__}: {e}"
            ),
        )

    teach_id = uuid.uuid4().hex[:8]
    stop_event = threading.Event()

    # Background pump: drain pending payloads + push live event count
    # to any subscribers (handled via the event broker below). Pump
    # ticks run on the SAME pool the setup used so Playwright thread
    # affinity is preserved.
    pump_task = asyncio.create_task(
        _teach_pump(recorder, stop_event, teach_id, pool)
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
        pool=pool,
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

    # Final drain + finalize on the SAME pool that setup ran on, so
    # sync_playwright thread affinity holds.
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

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_teach.pool, _finalize_in_worker)
    except Exception:
        pass
    finally:
        _teach.pool.shutdown(wait=False)

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
    recorder,
    stop_event: threading.Event,
    teach_id: str,
    pool,
) -> None:
    """Background drain. Calls recorder._drain_pending() periodically
    and publishes a count update so the UI can show "captured N
    events" in real time. Polls the page (via Playwright) to keep the
    sync_playwright event loop moving — without this the binding
    callbacks never fire.

    All Playwright work runs on ``pool``, the dedicated single-thread
    executor that owns the BrowserSession. asyncio.to_thread spawns a
    fresh thread each call which would violate sync_playwright thread
    affinity.
    """
    last_count = -1
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        try:
            await loop.run_in_executor(pool, _pump_tick, recorder)
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
# Replay endpoints — POST /api/tasks + POST /api/commands
# ---------------------------------------------------------------------------


def _build_orchestrator_for_task(portal_id: str | None):
    """Build an Orchestrator + AIClient + executor for a fresh task.
    Imports are lazy so server startup stays cheap when no replay has
    been requested yet."""
    from pilot.agent.ai_client import get_client
    from pilot.agent.executor_real import RealExecutor, RealExecutorConfig
    from pilot.agent.orchestrator import (
        Orchestrator,
        OrchestratorConfig,
    )
    from pilot.agent.schemas.portal_context import PortalContext

    client_name = os.environ.get("CURATIONPILOT_AI_CLIENT", "groq")
    try:
        client = get_client(client_name)
    except Exception:
        # Fall back to mock so the UI is at least navigable without a key.
        client = get_client("mock")

    portal_ctx: PortalContext | None = None
    if portal_id:
        ctx_dict = _read_portal_context(portal_id)
        if ctx_dict:
            try:
                portal_ctx = PortalContext.model_validate(ctx_dict)
            except Exception:
                portal_ctx = None

    config = OrchestratorConfig(
        sessions_dir=_sessions_dir(),
        skills_dir=_skills_dir(),
        portal_context=portal_ctx,
        portals_dir=_portals_dir(),
        auto_approve_plan=False,
    )

    # Use the real executor only when CDP is up. Otherwise FakeExecutor
    # gives the UI a meaningful event stream against the mock skill flow.
    executor = None
    if _launcher.state and _launcher.state.cdp_url:
        target_substring = None
        if portal_id:
            target_substring = _portal_base_url(portal_id)
        executor = RealExecutor(
            RealExecutorConfig(
                skills_dir=_skills_dir(),
                sessions_dir=_sessions_dir(),
                cdp_endpoint=_launcher.state.cdp_url,
                target_url_substring=target_substring,
            )
        )

    cmd_q: asyncio.Queue = asyncio.Queue()
    ev_q: asyncio.Queue = asyncio.Queue()
    orchestrator = Orchestrator(
        client=client,
        config=config,
        ev_out=ev_q,
        cmd_in=cmd_q,
        executor=executor,
    )
    return orchestrator, client, cmd_q, ev_q


async def _pump_orch_events(ev_q, task_id: str) -> None:
    """Forward every event from the orchestrator's queue onto the
    process-wide bus so /api/events subscribers see it. Stops when the
    task transitions to a terminal state (completed/failed/cancelled)."""
    terminal = {"task.completed", "task.failed", "task.cancelled"}
    while True:
        ev = await ev_q.get()
        try:
            payload = ev.model_dump(mode="json") if hasattr(ev, "model_dump") else dict(ev)
        except Exception:
            payload = {"type": "agent.log", "level": "warn", "message": str(ev)}
        await _bus.publish(payload)
        if payload.get("type") in terminal:
            return


@app.post("/api/tasks")
async def submit_task(
    goal: str = Form(...),
    portal_id: Optional[str] = Form(None),
    auto_approve_plan: bool = Form(False),
    attachments: list[UploadFile] = File(default=[]),
) -> dict:
    """Start a new replay task. Single-tenant: returns 409 if another
    task is already active. The UI subscribes to /api/events (already
    open) to receive every event the orchestrator emits."""
    global _active_task
    async with _active_task_lock:
        if _active_task is not None and not _active_task.finished:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"task {_active_task.task_id} is already active; "
                    "cancel it before submitting a new one"
                ),
            )

        from pilot.agent.schemas.protocol import (
            Attachment as PAttachment,
            TaskOptions,
            TaskSubmit,
        )

        task_id = uuid.uuid4().hex[:10]
        attach_dir = _sessions_dir() / "_attachments" / task_id
        attach_dir.mkdir(parents=True, exist_ok=True)

        saved: list[PAttachment] = []
        for f in attachments or []:
            if not f or not f.filename:
                continue
            # Defensive: strip directory parts so an upload can't escape
            # the attachment dir.
            safe_name = os.path.basename(f.filename)
            target = attach_dir / safe_name
            with target.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            kind = None
            ext = target.suffix.lower().lstrip(".")
            if ext in ("pptx", "csv", "pdf"):
                kind = ext
            elif ext in ("png", "jpg", "jpeg", "gif", "webp"):
                kind = "image"
            saved.append(PAttachment(path=str(target), kind=kind))

        orchestrator, client, cmd_q, ev_q = _build_orchestrator_for_task(
            portal_id
        )

        submit = TaskSubmit(
            task_id=task_id,
            goal=goal,
            attachments=saved,
            portal_id=portal_id,
            options=TaskOptions(auto_approve_plan=auto_approve_plan),
        )

        pump_task = asyncio.create_task(_pump_orch_events(ev_q, task_id))
        runner_task = asyncio.create_task(orchestrator.run_task(submit))

        _active_task = _ActiveTask(
            task_id=task_id,
            orchestrator=orchestrator,
            cmd_q=cmd_q,
            ev_q=ev_q,
            pump_task=pump_task,
            runner_task=runner_task,
            client=client,
            started_at=datetime.utcnow(),
            portal_id=portal_id,
        )

        # Spawn a small janitor that flips finished=true and closes the
        # client once the runner_task returns. Without this, a successful
        # task would block the slot until the next server restart.
        async def _on_finish():
            try:
                await runner_task
            except Exception:
                pass
            try:
                await pump_task
            except Exception:
                pass
            global _active_task
            if _active_task and _active_task.task_id == task_id:
                _active_task.finished = True
            try:
                await client.close()
            except Exception:
                pass

        asyncio.create_task(_on_finish())

        return {
            "task_id": task_id,
            "started_at": _active_task.started_at.isoformat(),
            "attachments": [a.path for a in saved],
        }


@app.post("/api/commands")
async def submit_command(payload: dict) -> dict:
    """Forward a host command (clarify.answer, plan.approve, plan.reject,
    pause.resolve, task.cancel) into the active orchestrator's queue.

    The payload is the JSON-RPC envelope documented in
    DOCS/PROTOCOL.md. This endpoint validates the shape via
    parse_host_command before queueing.
    """
    if _active_task is None or _active_task.finished:
        raise HTTPException(status_code=409, detail="no active task")
    from pilot.agent.schemas.protocol import parse_host_command

    try:
        cmd = parse_host_command(payload)
    except (ValueError, Exception) as e:
        raise HTTPException(
            status_code=400, detail=f"invalid host command: {e}"
        )
    cmd_task_id = getattr(cmd, "task_id", None)
    if cmd_task_id != _active_task.task_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"command targets task_id {cmd_task_id!r} but the "
                f"active task is {_active_task.task_id!r}"
            ),
        )
    await _active_task.cmd_q.put(cmd)
    return {"ok": True, "queued": cmd.type}


@app.get("/api/tasks/active")
async def get_active_task() -> dict:
    """Return a summary of the in-flight task, or {active: false}."""
    if _active_task is None or _active_task.finished:
        return {"active": False}
    return {
        "active": True,
        "task_id": _active_task.task_id,
        "portal_id": _active_task.portal_id,
        "started_at": _active_task.started_at.isoformat(),
    }


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
