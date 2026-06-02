"""CLI entry point for the POC.

Usage:
    python -m pilot doctor
    python -m pilot run sample_tasks/add_and_verify.json
    python -m pilot teach my_skill --base-url http://localhost:5188
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
        "http://localhost:5188",
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
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        help=(
            "Portal base URL — used to pick the right Chrome tab. "
            "Either --base-url or --portal-id must be provided. If both, "
            "--base-url wins."
        ),
    ),
    cdp: str = typer.Option(DEFAULT_CDP_ENDPOINT, "--cdp"),
    sessions_dir: Path = typer.Option(Path("sessions"), "--sessions-dir"),
    portal_id: Optional[str] = typer.Option(
        None,
        "--portal-id",
        help=(
            "Portal id (e.g. 'sample_portal'). Resolves base_url from "
            "portals/<portal_id>/context.yaml. Page snapshots from this "
            "session also merge into portals/<portal_id>/catalog.yaml."
        ),
    ),
    portals_dir: Path = typer.Option(Path("portals"), "--portals-dir"),
) -> None:
    """Start a passive teach recording. Use the portal; press Ctrl+C to stop."""
    if not base_url and portal_id:
        base_url = _resolve_portal_base_url(portals_dir, portal_id)
        if not base_url:
            console.print(
                f"[red]Portal '{portal_id}' has no base_url[/red] in "
                f"{portals_dir}/{portal_id}/context.yaml. Either fix the "
                "context file or pass --base-url explicitly."
            )
            raise typer.Exit(code=2)
    if not base_url:
        console.print(
            "[red]Specify either --portal-id or --base-url[/red]. "
            "There is no default — CurationPilot drives any portal, so "
            "the URL has to come from somewhere intentional."
        )
        raise typer.Exit(code=2)

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


def _resolve_portal_base_url(portals_dir: Path, portal_id: str) -> Optional[str]:
    """Read base_url from portals/<id>/context.yaml. Returns None if
    the portal is unknown or has no base_url declared."""
    path = portals_dir / portal_id / "context.yaml"
    if not path.exists():
        return None
    try:
        import yaml

        ctx = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        url = ctx.get("base_url")
        return url if isinstance(url, str) and url else None
    except Exception:
        return None


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


def _prompt_clarify_for_missing_params(
    skill_path: Path,
    params: dict,
    *,
    input_fn=input,
    output=None,
) -> dict:
    """2026-06-02 final batch item 5: interactive clarify for missing
    required params at CLI replay time.

    Loads the skill's params metadata (label_options, accessible_name,
    depends_on) and for each required param that isn't already in
    ``params``, prompts the operator on stdin with a numbered choice
    list (from label_options) plus a free-form option when
    allow_custom_answer is set.

    Resolves the operator's typed label to the underlying value via the
    skill's enum_options / known_options when possible -- otherwise the
    label passes through and the runner's refresh_options_after path
    reconciles against live options (item 2).

    ``input_fn`` / ``output`` injection support unit-testing without
    sys.stdin / console state.
    """
    out_print = (output if output is not None else console.print)
    # Load the skill JSON and inspect its declared params.
    try:
        skill_dict = json.loads(skill_path.read_text(encoding="utf-8"))
    except Exception:
        return params
    declared = skill_dict.get("params") or skill_dict.get("parameters") or []

    # Build ordered list, parents first (topological).
    by_name = {p.get("name"): p for p in declared if p.get("name")}
    ordered: list[dict] = []
    seen: set[str] = set()

    def _emit(p: dict) -> None:
        name = p.get("name")
        if not name or name in seen:
            return
        for parent in (p.get("depends_on") or []):
            if isinstance(parent, str):
                parent_p = by_name.get(parent)
                if parent_p and parent not in seen:
                    _emit(parent_p)
        seen.add(name)
        ordered.append(p)

    for p in declared:
        _emit(p)

    resolved = dict(params)
    for p in ordered:
        name = p.get("name")
        if not name or not p.get("required", True):
            continue
        if resolved.get(name) not in (None, "", [], {}):
            continue

        # Build the option universe. For v1 skills the universe lives
        # under enum_options (single-select) or set_selection
        # known_options on the matching step. For v2 schema params it
        # lives under label_options.
        label_options: list[str] = []
        enum_options = p.get("enum_options") or []
        if isinstance(enum_options, list):
            for opt in enum_options:
                if isinstance(opt, dict):
                    lbl = opt.get("label") or opt.get("value")
                    if lbl:
                        label_options.append(str(lbl))
        for lbl in (p.get("label_options") or []):
            if isinstance(lbl, str) and lbl not in label_options:
                label_options.append(lbl)
        # 2026-06-02 final batch v1: also pull set_selection step's
        # known_options for string_list params (Country/Region pickers
        # capture options across the cluster).
        if p.get("type") == "string_list" and not label_options:
            for step in (skill_dict.get("steps") or []):
                if step.get("action") == "set_selection":
                    ss = step.get("set_selection") or {}
                    if ss.get("param") == name:
                        for opt in (ss.get("known_options") or []):
                            if isinstance(opt, dict):
                                lbl = opt.get("label") or opt.get("value")
                                if lbl:
                                    label_options.append(str(lbl))
        # Same for select_option steps.
        if not label_options:
            for step in (skill_dict.get("steps") or []):
                if step.get("action") == "select_option":
                    binding = step.get("param_binding") or {}
                    if binding.get("name") == name:
                        so = step.get("select_option") or {}
                        for opt in (so.get("known_options") or []):
                            if isinstance(opt, dict):
                                lbl = opt.get("label") or opt.get("value")
                                if lbl:
                                    label_options.append(str(lbl))

        # Find human label for the question.
        human_label = (
            p.get("accessible_name")
            or p.get("semantic")
            or p.get("description")
            or name.replace("_", " ").title()
        )
        import re as _re
        human_label = _re.sub(r"[*:\s]+$", "", str(human_label)).strip()

        out_print(f"\n[bold]Which {human_label}?[/bold]")
        # Cap visible options at 15; allow custom answer beyond that.
        cap = 15
        allow_custom = len(label_options) > cap or len(label_options) == 0
        visible = label_options[:cap]
        for i, lbl in enumerate(visible, start=1):
            out_print(f"  [{i}] {lbl}")
        prompt_suffix = (
            " (number, label, or type your own): "
            if allow_custom
            else " (number or label): "
        )
        try:
            answer = input_fn(f"  >{prompt_suffix}").strip()
        except EOFError:
            answer = ""
        if not answer:
            out_print(
                f"[red]No answer given for required param {name!r}.[/red]"
            )
            raise typer.Exit(code=2)
        # Resolve number choice if possible.
        chosen_label: str
        if answer.isdigit() and visible:
            idx = int(answer)
            if 1 <= idx <= len(visible):
                chosen_label = visible[idx - 1]
            else:
                chosen_label = answer
        else:
            chosen_label = answer

        # For string_list params (set_selection), wrap in a list.
        if p.get("type") == "string_list":
            resolved[name] = [chosen_label]
        else:
            resolved[name] = chosen_label
        out_print(f"  -> {name} = {chosen_label!r}")
    return resolved


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
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        help=(
            "Override the skill's recorded base_url. Usually unnecessary — "
            "skills/<name>.json already carries the URL the recording was "
            "made against."
        ),
    ),
    cdp: str = typer.Option(DEFAULT_CDP_ENDPOINT, "--cdp"),
    sessions_dir: Path = typer.Option(Path("sessions"), "--sessions-dir"),
    no_clarify: bool = typer.Option(
        False,
        "--no-clarify",
        help=(
            "Skip the interactive clarify prompt for missing required "
            "params. Use for scripted runs that must fail-fast when a "
            "param is absent."
        ),
    ),
) -> None:
    """Replay a learned skill with parameters.

    The portal URL comes from the skill's own ``base_url`` field —
    skills are tied to the portal they were recorded against. Override
    only when you know what you're doing (e.g. replaying a staging-env
    recording against prod).

    2026-06-02 final batch item 5: when required params are missing AND
    the skill has captured label_options / known_options, the CLI
    prompts the operator on stdin with the available choices. Pass
    --no-clarify to fail-fast instead (scripted runs)."""
    params: dict = {}
    if params_file and params_file.exists():
        params.update(json.loads(params_file.read_text(encoding="utf-8")))
    for p in param:
        if "=" not in p:
            console.print(f"[red]Bad --param:[/red] {p}")
            raise typer.Exit(code=2)
        k, _, v = p.partition("=")
        params[k] = v

    # 2026-06-02 final batch item 5: interactive clarify for missing
    # required params (skip when --no-clarify is set, for scripted runs).
    if not no_clarify:
        params = _prompt_clarify_for_missing_params(skill_path, params)

    try:
        # Resolve base_url: explicit --base-url wins; otherwise read
        # from the skill's own base_url. If neither is present, the
        # runner will refuse — there's no implicit "sample portal"
        # default any more.
        effective_base_url = base_url
        if not effective_base_url:
            try:
                skill_dict = json.loads(skill_path.read_text(encoding="utf-8"))
                effective_base_url = skill_dict.get("base_url") or None
            except Exception:
                effective_base_url = None
        if not effective_base_url:
            console.print(
                "[red]No base_url available[/red] — the skill JSON has none "
                "and you didn't pass --base-url. Re-record or supply one."
            )
            raise typer.Exit(code=2)

        results = run_skill_from_file(
            skill_path=skill_path,
            params=params,
            base_url=effective_base_url,
            cdp=cdp,
            sessions_dir=sessions_dir,
        )
    except Exception as e:
        console.print(f"[red]Replay failed:[/red] {e}")
        raise typer.Exit(code=2)

    failed = [r for (_, r, _) in results if not r.success]
    if failed:
        raise typer.Exit(code=1)


@app.command("upgrade-skill")
def upgrade_skill_cmd(
    skill_path: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        writable=True,
        help="Path to a legacy skill JSON. Upgraded in place.",
    ),
) -> None:
    """Upgrade a legacy v1 skill JSON to the current schema (v2).

    Idempotent: re-running on an already-upgraded skill is a no-op
    structurally but rewrites the file with the same content. Use this
    once-per-skill to migrate pre-sprint recordings into a shape the
    current SkillRunner consumes natively.
    """
    from .skill_upgrade import CURRENT_SCHEMA_VERSION, upgrade_skill_file

    try:
        upgraded = upgrade_skill_file(skill_path)
    except Exception as e:
        console.print(f"[red]Upgrade failed:[/red] {e}")
        raise typer.Exit(code=2)
    from_version = upgraded.get("upgraded_from")
    if from_version is not None:
        console.print(
            f"[green]Upgraded[/green] {skill_path.name} "
            f"v{from_version} -> v{CURRENT_SCHEMA_VERSION}."
        )
    else:
        console.print(
            f"[green]No-op[/green]: {skill_path.name} already at "
            f"v{CURRENT_SCHEMA_VERSION}."
        )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(5177, "--port"),
) -> None:
    """Start the CurationPilot UI server (FastAPI + WebSocket).

    The React app at curationpilot-app/ proxies /api/* to this server
    in dev (vite.config.ts). In prod, serve the built dist/ from the
    same origin to avoid CORS.
    """
    from pilot.agent.web_server import serve as _serve

    _serve(host=host, port=port)


if __name__ == "__main__":
    app()
