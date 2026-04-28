"""Terminal driver for the agent.

Same Orchestrator as the JSON-RPC server, but the host loop renders
events to a Rich terminal UI and reads operator answers from stdin.

Usage:
    python -m pilot.agent.cli do "Curate this release" --attach release.pptx
                                                       --attach thumbs/
                                                       --portal sample_portal
                                                       --client mock

Subcommands:
    do <goal>        Run a full task end-to-end.
    intake <goal>    Run only the intake stage; print entities as JSON.
    plan <goal>      Run intake + planner; print the proposed plan.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pilot.agent.ai_client import get_client
from pilot.agent.intake import run_intake
from pilot.agent.orchestrator import Orchestrator, OrchestratorConfig
from pilot.agent.planner import load_skill_library, run_planner
from pilot.agent.schemas.portal_context import PortalContext
from pilot.agent.schemas.protocol import (
    Attachment,
    ClarifyAnswer as ClarifyAnswerCmd,
    ClarifyAsk,
    HostCommand,
    IntakeExtracted,
    PauseResolve,
    Paused,
    PlanApprove,
    PlanProposed,
    PlanReject,
    ReportReady,
    StepFailedEvent,
    StepProgressEvent,
    StepStartedEvent,
    StepSucceededEvent,
    TaskCompleted,
    TaskFailed,
    TaskSubmit,
    AgentEvent,
)


console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_portal_context(portal_id: str | None) -> PortalContext | None:
    if not portal_id:
        return None
    p = Path("portals") / portal_id / "context.yaml"
    if not p.exists():
        console.print(f"[yellow]warning:[/yellow] portal context not found at {p}")
        return None
    return PortalContext.model_validate(yaml.safe_load(p.read_text()))


def _attach_args(values: list[str]) -> list[Attachment]:
    out: list[Attachment] = []
    for v in values:
        kind: str | None = None
        path = v
        if v.endswith(".pptx"):
            kind = "pptx"
        elif v.endswith(".csv"):
            kind = "csv"
        elif Path(v).is_dir():
            kind = "folder"
        out.append(Attachment(path=path, kind=kind))
    return out


def _read_choice(prompt: str, options: list[tuple[str, str]]) -> str:
    """Prompt for one of the given (value,label) options. Returns chosen value."""
    console.print(prompt)
    for i, (val, label) in enumerate(options, start=1):
        console.print(f"  [bold]{i}[/bold]) {label}  [dim]({val})[/dim]")
    console.print("  [bold]c[/bold]) custom answer")
    while True:
        choice = console.input("  > ").strip().lower()
        if choice in ("c", "custom"):
            return console.input("  custom: ").strip()
        try:
            idx = int(choice)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        except ValueError:
            pass
        console.print("[red]please pick a number from the list, or 'c'[/red]")


# ---------------------------------------------------------------------------
# Event renderer / host loop
# ---------------------------------------------------------------------------


async def _drive_orchestrator(
    *,
    orch: Orchestrator,
    submit: TaskSubmit,
    auto_approve: bool,
) -> int:
    """Run the orchestrator and render its events. Returns exit code."""
    ev_q = orch.ev_out
    cmd_q = orch.cmd_in

    async def _renderer() -> int:
        """Render events. Submits host commands when prompted."""
        exit_code = 0
        while True:
            ev = await ev_q.get()
            t = ev.type

            if t == "intake.extracted":
                e: IntakeExtracted = ev  # type: ignore[assignment]
                console.rule("[cyan]intake[/cyan]")
                console.print(
                    f"  items: {len(e.entities.content_items)}  "
                    f"dates: {len(e.entities.dates)}  "
                    f"files: {len(e.entities.files_resolved)}"
                )
                for w in e.intake_warnings:
                    console.print(f"  [yellow]warn:[/yellow] {w}")

            elif t == "clarify.ask":
                ca: ClarifyAsk = ev  # type: ignore[assignment]
                console.rule("[magenta]clarify[/magenta]")
                console.print(Panel(ca.question, title=f"id={ca.id}"))
                if ca.options:
                    answer = _read_choice("  pick one:", [(o.value, o.label) for o in ca.options])
                else:
                    answer = console.input("  your answer: ").strip()
                await cmd_q.put(
                    ClarifyAnswerCmd(
                        task_id=submit.task_id,
                        question_id=ca.id,
                        answer_value=answer,
                        answer_label=answer,
                    )
                )

            elif t == "plan.proposed":
                pp: PlanProposed = ev  # type: ignore[assignment]
                console.rule("[green]plan proposed[/green]")
                console.print(Panel(pp.summary, title=f"id={pp.id}"))
                tbl = Table(title="steps")
                tbl.add_column("#")
                tbl.add_column("skill")
                tbl.add_column("params")
                tbl.add_column("source")
                for s in pp.steps:
                    params_str = ", ".join(f"{k}={v!r}" for k, v in list(s.params.items())[:4])
                    src_str = ", ".join(f"{k}<-{v}" for k, v in list(s.param_sources.items())[:2])
                    tbl.add_row(str(s.idx), s.skill_id, params_str, src_str)
                console.print(tbl)
                if pp.destructive_actions:
                    console.print("[red]destructive actions:[/red]")
                    for da in pp.destructive_actions:
                        console.print(
                            f"  - step {da.step_idx} {da.kind} "
                            f"(reversible={da.reversible})"
                        )
                console.print(f"[dim]estimated {pp.estimated_duration_seconds}s[/dim]")
                if auto_approve:
                    console.print("[dim](auto-approve)[/dim]")
                    await cmd_q.put(
                        PlanApprove(task_id=submit.task_id, plan_id=pp.id)
                    )
                else:
                    answer = console.input("  approve? [y/N] ").strip().lower()
                    if answer in ("y", "yes"):
                        await cmd_q.put(
                            PlanApprove(task_id=submit.task_id, plan_id=pp.id)
                        )
                    else:
                        reason = console.input("  reject reason (optional): ").strip()
                        await cmd_q.put(
                            PlanReject(
                                task_id=submit.task_id,
                                plan_id=pp.id,
                                reason=reason or "operator declined",
                            )
                        )

            elif t == "step.started":
                ss: StepStartedEvent = ev  # type: ignore[assignment]
                console.print(f"[bold]>[/bold] step {ss.idx} [{ss.skill_id}] starting")

            elif t == "step.progress":
                sp: StepProgressEvent = ev  # type: ignore[assignment]
                detail = f"{sp.action}"
                if sp.test_id:
                    detail += f" {sp.test_id}"
                console.print(f"  [dim]·[/dim] {detail}")

            elif t == "step.succeeded":
                sc: StepSucceededEvent = ev  # type: ignore[assignment]
                console.print(
                    f"[green]✓[/green] step {sc.idx} succeeded ({sc.duration_ms}ms)"
                )

            elif t == "step.failed":
                sf: StepFailedEvent = ev  # type: ignore[assignment]
                console.print(
                    f"[red]✗[/red] step {sf.idx} failed: "
                    f"{sf.error_kind} — {sf.error_message}"
                )

            elif t == "paused":
                pz: Paused = ev  # type: ignore[assignment]
                console.rule("[yellow]paused[/yellow]")
                opts = [
                    ("retry", "Retry this step"),
                    ("skip", "Skip and continue"),
                    ("abort", "Abort the run"),
                ]
                action = _read_choice(
                    f"  reason: {pz.reason}\n  pick action:", opts
                )
                await cmd_q.put(
                    PauseResolve(
                        task_id=submit.task_id,
                        pause_id=pz.pause_id,
                        action=action,  # type: ignore[arg-type]
                    )
                )

            elif t == "report.ready":
                rr: ReportReady = ev  # type: ignore[assignment]
                console.rule("[cyan]report[/cyan]")
                console.print(rr.summary)
                console.print(f"  written to: {rr.report_path}")

            elif t == "task.completed":
                tc: TaskCompleted = ev  # type: ignore[assignment]
                console.rule("[bold green]task complete[/bold green]")
                console.print(f"  session: {tc.session_id}")
                return 0

            elif t == "task.failed":
                tf: TaskFailed = ev  # type: ignore[assignment]
                console.rule("[bold red]task failed[/bold red]")
                console.print(f"  {tf.error_kind}: {tf.error_message}")
                return 1

            elif t == "task.cancelled":
                console.rule("[yellow]cancelled[/yellow]")
                return 130

            elif t == "agent.log":
                # Render warnings/errors only; debug/info are noise on TTY.
                level = getattr(ev, "level", "info")
                if level in ("warn", "error"):
                    console.print(f"[dim][{level}][/dim] {ev.message}")

            # silently ignore other events (heartbeats etc.)

    runner_task = asyncio.create_task(orch.run_task(submit))
    renderer_task = asyncio.create_task(_renderer())

    done, pending = await asyncio.wait(
        {runner_task, renderer_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for p in pending:
        p.cancel()
    for d in done:
        exc = d.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            console.print(f"[red]agent error:[/red] {exc}")
            return 1
    if renderer_task in done:
        return renderer_task.result()
    return 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _make_orchestrator(args: argparse.Namespace) -> Orchestrator:
    client = get_client(args.client)
    portal_ctx = _load_portal_context(args.portal)
    config = OrchestratorConfig(
        sessions_dir=Path(args.sessions_dir),
        skills_dir=Path(args.skills_dir),
        portal_context=portal_ctx,
        intake_use_llm=not args.no_llm_intake,
        auto_approve_plan=args.auto_approve,
    )
    ev_q: asyncio.Queue = asyncio.Queue()
    cmd_q: asyncio.Queue = asyncio.Queue()
    return Orchestrator(client=client, config=config, ev_out=ev_q, cmd_in=cmd_q)


async def _cmd_do(args: argparse.Namespace) -> int:
    orch = _make_orchestrator(args)
    submit = TaskSubmit(
        task_id=f"t-{uuid.uuid4().hex[:6]}",
        goal=args.goal,
        attachments=_attach_args(args.attach or []),
        portal_id=args.portal,
    )
    return await _drive_orchestrator(
        orch=orch, submit=submit, auto_approve=args.auto_approve
    )


async def _cmd_intake(args: argparse.Namespace) -> int:
    client = get_client(args.client)
    entities = await run_intake(
        client=client,
        goal=args.goal,
        attachments=_attach_args(args.attach or []),
        use_llm=not args.no_llm_intake,
    )
    print(entities.model_dump_json(indent=2))
    await client.close()
    return 0


async def _cmd_plan(args: argparse.Namespace) -> int:
    client = get_client(args.client)
    portal_ctx = _load_portal_context(args.portal)
    skills = load_skill_library(Path(args.skills_dir))

    entities = await run_intake(
        client=client,
        goal=args.goal,
        attachments=_attach_args(args.attach or []),
        use_llm=not args.no_llm_intake,
    )
    out = await run_planner(
        client=client,
        goal=args.goal,
        entities=entities,
        skills=skills,
        portal=portal_ctx,
    )
    print(json.dumps(
        {
            "plan": out.plan.model_dump() if out.plan else None,
            "clarify_questions": [q.model_dump() for q in out.clarify_questions],
            "notes": out.notes,
        },
        indent=2,
        default=str,
    ))
    await client.close()
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("goal", help="natural-language goal")
    p.add_argument(
        "--attach", action="append", default=[],
        help="path to a file or folder; can be passed multiple times",
    )
    p.add_argument("--portal", default=None, help="portal_id to load context for")
    p.add_argument("--client", default="mock", help="AIClient name (default: mock)")
    p.add_argument("--sessions-dir", default="sessions")
    p.add_argument("--skills-dir", default="skills")
    p.add_argument("--no-llm-intake", action="store_true")
    p.add_argument("--auto-approve", action="store_true",
                   help="bypass plan approval (test only)")


def main() -> int:
    parser = argparse.ArgumentParser(prog="pilot.agent.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_do = sub.add_parser("do", help="run intake -> plan -> approve -> execute -> report")
    _add_common(p_do)
    p_intake = sub.add_parser("intake", help="run only the intake stage")
    _add_common(p_intake)
    p_plan = sub.add_parser("plan", help="run intake + planner, print plan")
    _add_common(p_plan)

    args = parser.parse_args()
    try:
        if args.cmd == "do":
            return asyncio.run(_cmd_do(args))
        if args.cmd == "intake":
            return asyncio.run(_cmd_intake(args))
        if args.cmd == "plan":
            return asyncio.run(_cmd_plan(args))
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
