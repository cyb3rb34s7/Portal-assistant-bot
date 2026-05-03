"""CLI entry point for the POC.

Usage:
    python -m pilot doctor
    python -m pilot run sample_tasks/add_and_verify.json
    python -m pilot teach my_skill --base-url http://localhost:5173
    python -m pilot annotate <session_id> --auto
    python -m pilot run-skill skills/my_skill.json --param content_id=A-3001
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .annotate import run_annotate
from .browser import DEFAULT_CDP_ENDPOINT, connect_to_chrome
from .models import TaskList
from .runner import Runner
from .skill_runner import run_skill_from_file
from .teach import run_teach


app = typer.Typer(add_completion=False, help="CurationPilot POC runner")
console = Console()


@app.command()
def run(
    task_file: Path = typer.Argument(
        ..., exists=True, readable=True, help="Path to a TaskList JSON file"
    ),
    base_url: str = typer.Option(
        "http://localhost:5173",
        "--base-url",
        help="Sample portal base URL",
    ),
    cdp: str = typer.Option(
        DEFAULT_CDP_ENDPOINT,
        "--cdp",
        help="Chrome DevTools Protocol endpoint",
    ),
    sessions_dir: Path = typer.Option(
        Path("sessions"),
        "--sessions-dir",
        help="Directory where session artifacts are written",
    ),
) -> None:
    """Run a hand-written task list (legacy deterministic runner)."""
    sessions_dir.mkdir(parents=True, exist_ok=True)

    raw = json.loads(task_file.read_text())
    task_list = TaskList.model_validate(raw)

    console.print(f"Connecting to Chrome at [bold]{cdp}[/bold] ...")
    try:
        session = connect_to_chrome(cdp, target_url_substring=base_url)
    except Exception as e:
        console.print(f"[red]Failed to connect to Chrome:[/red] {e}")
        console.print(
            "Start Chrome with [bold]--remote-debugging-port=9222[/bold] "
            "and open the sample portal, then retry."
        )
        raise typer.Exit(code=2)

    try:
        runner = Runner(
            session=session,
            task_list=task_list,
            sessions_dir=sessions_dir,
            base_url=base_url,
        )
        runner.run()
    finally:
        session.close()


@app.command()
def doctor(
    cdp: str = typer.Option(DEFAULT_CDP_ENDPOINT, "--cdp"),
) -> None:
    """Quick feasibility check for CDP connectivity."""
    try:
        session = connect_to_chrome(cdp)
    except Exception as e:
        console.print(f"[red]CDP connect failed:[/red] {e}")
        raise typer.Exit(code=1)
    pages = [p.url for p in session.context.pages]
    console.print(f"[green]Connected.[/green] {len(pages)} page(s):")
    for url in pages:
        console.print(f"  - {url}")
    session.close()


@app.command()
def teach(
    skill_name: str = typer.Argument(..., help="Name of the skill to teach"),
    base_url: str = typer.Option(
        "http://localhost:5173",
        "--base-url",
        help="Portal base URL (used to pick the right Chrome tab)",
    ),
    cdp: str = typer.Option(DEFAULT_CDP_ENDPOINT, "--cdp"),
    sessions_dir: Path = typer.Option(Path("sessions"), "--sessions-dir"),
    portal_id: Optional[str] = typer.Option(
        None,
        "--portal-id",
        help=(
            "Portal id (e.g. 'sample_portal'). When set, page snapshots "
            "from this teach session merge into "
            "portals/<portal_id>/catalog.yaml — the passive catalog."
        ),
    ),
    portals_dir: Path = typer.Option(Path("portals"), "--portals-dir"),
) -> None:
    """Start a passive teach recording. Use the portal; press Ctrl+C to stop."""
    try:
        sid = run_teach(
            skill_name=skill_name,
            base_url=base_url,
            cdp=cdp,
            sessions_dir=sessions_dir,
            portal_id=portal_id,
            portals_dir=portals_dir,
        )
        console.print(f"[green]Session complete:[/green] {sid}")
    except Exception as e:
        console.print(f"[red]Teach failed:[/red] {e}")
        raise typer.Exit(code=2)


@app.command()
def annotate(
    session_id: str = typer.Argument(..., help="Session id from a teach run"),
    skill_name: Optional[str] = typer.Option(None, "--name", help="Override skill name"),
    description: str = typer.Option("", "--description"),
    base_url: Optional[str] = typer.Option(None, "--base-url"),
    portal: Optional[str] = typer.Option(None, "--portal"),
    auto: bool = typer.Option(
        False, "--auto", help="Apply heuristic defaults without prompting"
    ),
    sessions_dir: Path = typer.Option(Path("sessions"), "--sessions-dir"),
    skills_dir: Path = typer.Option(Path("skills"), "--skills-dir"),
) -> None:
    """Convert a recorded trace into a skill JSON."""
    run_annotate(
        session_id=session_id,
        skill_name=skill_name,
        description=description,
        base_url=base_url,
        portal=portal,
        auto=auto,
        sessions_dir=sessions_dir,
        skills_dir=skills_dir,
    )


@app.command("run-skill")
def run_skill(
    skill_path: Path = typer.Argument(..., exists=True, readable=True),
    param: list[str] = typer.Option(
        [],
        "--param",
        "-p",
        help="Parameter in name=value form. Repeatable.",
    ),
    params_file: Optional[Path] = typer.Option(
        None, "--params-file", help="JSON file of parameters"
    ),
    base_url: str = typer.Option("http://localhost:5173", "--base-url"),
    cdp: str = typer.Option(DEFAULT_CDP_ENDPOINT, "--cdp"),
    sessions_dir: Path = typer.Option(Path("sessions"), "--sessions-dir"),
) -> None:
    """Replay a learned skill with parameters."""
    params: dict = {}
    if params_file and params_file.exists():
        params.update(json.loads(params_file.read_text(encoding="utf-8")))
    for p in param:
        if "=" not in p:
            console.print(f"[red]Bad --param:[/red] {p}")
            raise typer.Exit(code=2)
        k, _, v = p.partition("=")
        params[k] = v

    try:
        results = run_skill_from_file(
            skill_path=skill_path,
            params=params,
            base_url=base_url,
            cdp=cdp,
            sessions_dir=sessions_dir,
        )
    except Exception as e:
        console.print(f"[red]Replay failed:[/red] {e}")
        raise typer.Exit(code=2)

    failed = [r for (_, r, _) in results if not r.success]
    if failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
