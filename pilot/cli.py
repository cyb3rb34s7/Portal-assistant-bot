"""CLI entry point for the POC.

Usage:
    python -m pilot run sample_tasks/add_and_verify.json \
        --base-url http://localhost:5173 \
        --cdp http://localhost:9222
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from .browser import DEFAULT_CDP_ENDPOINT, connect_to_chrome
from .models import TaskList
from .runner import Runner


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
    """Run a task list against the operator's Chrome via CDP."""
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


if __name__ == "__main__":
    app()
