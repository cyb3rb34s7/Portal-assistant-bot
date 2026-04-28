"""JSON-RPC stdio server.

Reads NDJSON commands from stdin, dispatches them to the
``Orchestrator``, and writes NDJSON events to stdout. See
``DOCS/PROTOCOL.md`` for the wire format.

Usage:
    python -m pilot.agent.server --portal sample_portal --client mock
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

from pilot.agent.ai_client import get_client
from pilot.agent.orchestrator import Orchestrator, OrchestratorConfig
from pilot.agent.schemas.portal_context import PortalContext
from pilot.agent.schemas.protocol import (
    AgentCapabilities,
    AgentEvent,
    AgentReady,
    HostCommand,
    TaskSubmit,
    parse_host_command,
)


AGENT_VERSION = "0.1.0"


def _load_portal_context(portal_id: str | None) -> PortalContext | None:
    if not portal_id:
        return None
    candidate = Path("portals") / portal_id / "context.yaml"
    if not candidate.exists():
        print(
            json.dumps(
                {
                    "v": 1,
                    "type": "agent.log",
                    "level": "warn",
                    "message": f"portal context file not found: {candidate}",
                }
            ),
            flush=True,
        )
        return None
    raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    return PortalContext.model_validate(raw)


async def _stdin_reader(cmd_q: asyncio.Queue[HostCommand]) -> None:
    """Pump NDJSON lines from stdin into the command queue."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.decode("utf-8").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                cmd = parse_host_command(payload)
            except (json.JSONDecodeError, ValueError) as e:
                print(
                    json.dumps(
                        {
                            "v": 1,
                            "type": "agent.log",
                            "level": "warn",
                            "message": f"unparseable host command: {e}",
                        }
                    ),
                    flush=True,
                )
                continue
            await cmd_q.put(cmd)
    finally:
        transport.close()


async def _stdout_writer(ev_q: asyncio.Queue[AgentEvent]) -> None:
    """Drain events from the queue and write them as NDJSON to stdout."""
    while True:
        ev = await ev_q.get()
        sys.stdout.write(ev.model_dump_json() + "\n")
        sys.stdout.flush()


async def _heartbeat(ev_q: asyncio.Queue[AgentEvent], interval_s: float = 30.0) -> None:
    from pilot.agent.schemas.protocol import AgentHeartbeat

    while True:
        await asyncio.sleep(interval_s)
        hb = AgentHeartbeat()
        hb.stamp()
        await ev_q.put(hb)


async def _main(args: argparse.Namespace) -> int:
    client = get_client(args.client)
    portal_ctx = _load_portal_context(args.portal)
    sessions_dir = Path(args.sessions_dir)
    skills_dir = Path(args.skills_dir)

    config = OrchestratorConfig(
        sessions_dir=sessions_dir,
        skills_dir=skills_dir,
        portal_context=portal_ctx,
        intake_use_llm=not args.no_llm_intake,
        auto_approve_plan=args.auto_approve,
    )

    ev_q: asyncio.Queue[AgentEvent] = asyncio.Queue()
    cmd_q: asyncio.Queue[HostCommand] = asyncio.Queue()

    orchestrator = Orchestrator(
        client=client, config=config, ev_out=ev_q, cmd_in=cmd_q
    )

    # Announce
    ready = AgentReady(
        agent_version=AGENT_VERSION,
        capabilities=AgentCapabilities(
            ai_clients=[client.name],
            default_client=client.name,
            supports_attachments=["pptx", "csv", "folder", "image", "pdf"],
        ),
    )
    ready.stamp()
    await ev_q.put(ready)

    writer_task = asyncio.create_task(_stdout_writer(ev_q))
    reader_task = asyncio.create_task(_stdin_reader(cmd_q))
    heartbeat_task = asyncio.create_task(_heartbeat(ev_q))

    try:
        # Single-task at a time (per protocol).
        while True:
            cmd = await cmd_q.get()
            if not isinstance(cmd, TaskSubmit):
                # Pre-task commands other than submit are dropped with a log
                continue
            await orchestrator.run_task(cmd)
    finally:
        for t in (writer_task, reader_task, heartbeat_task):
            t.cancel()
        await client.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CurationPilot agent JSON-RPC server (stdio)."
    )
    parser.add_argument("--client", default="mock", help="AIClient name (default: mock)")
    parser.add_argument("--portal", default=None, help="portal_id to load context for")
    parser.add_argument("--sessions-dir", default="sessions")
    parser.add_argument("--skills-dir", default="skills")
    parser.add_argument(
        "--no-llm-intake",
        action="store_true",
        help="Skip the LLM refinement step in intake (deterministic only).",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Bypass plan approval (testing only).",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
