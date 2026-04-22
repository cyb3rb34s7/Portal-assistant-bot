"""Deterministic task runner with human gates.

This is the minimal stand-in for LangGraph in the POC. It runs a list of
tasks sequentially, enforcing dependencies, pausing at approval gates,
and recording everything to the audit log. It is intentionally small —
the architecture doc describes promoting this to a LangGraph subgraph
later without changing the adapter code.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .adapters import BaseAdapter, MediaAssetsAdapter
from .audit import AuditLogger
from .browser import BrowserSession
from .models import Task, TaskList, ToolResult


ADAPTER_REGISTRY: dict[str, type[BaseAdapter]] = {
    "media_assets": MediaAssetsAdapter,
}


class Runner:
    def __init__(
        self,
        session: BrowserSession,
        task_list: TaskList,
        sessions_dir: Path,
        base_url: str,
        approve_fn: Optional[Callable[[Task], bool]] = None,
        console: Optional[Console] = None,
    ):
        self.session = session
        self.task_list = task_list
        self.session_id = uuid.uuid4().hex[:12]
        self.audit = AuditLogger(self.session_id, sessions_dir)
        self.base_url = base_url
        self.approve_fn = approve_fn or _cli_approve
        self.console = console or Console()
        self.adapters: dict[str, BaseAdapter] = {}
        self._results: dict[str, ToolResult] = {}

    def _get_adapter(self, adapter_name: str) -> BaseAdapter:
        if adapter_name in self.adapters:
            return self.adapters[adapter_name]
        cls = ADAPTER_REGISTRY.get(adapter_name)
        if cls is None:
            raise ValueError(f"Unknown adapter: {adapter_name}")
        adapter = cls(
            page=self.session.page, audit=self.audit, base_url=self.base_url
        )
        self.adapters[adapter_name] = adapter
        return adapter

    def run(self) -> dict[str, ToolResult]:
        self.console.print(
            Panel.fit(
                f"[bold]Session[/bold] {self.session_id}\n"
                f"Task list: {self.task_list.name}\n"
                f"Tasks: {len(self.task_list.tasks)}",
                title="CurationPilot",
            )
        )
        self.audit.log(
            "info",
            "session started",
            data={"task_list": self.task_list.name},
        )

        for task in self.task_list.tasks:
            if not self._deps_satisfied(task):
                task.status = "skipped"
                self.audit.log(
                    "info",
                    f"task {task.task_id} skipped: unmet dependencies",
                    task_id=task.task_id,
                )
                continue

            if task.requires_approval_gate:
                approved = self._run_gate(task)
                if not approved:
                    task.status = "rejected"
                    self.audit.log(
                        "gate",
                        f"task {task.task_id} rejected at approval gate",
                        task_id=task.task_id,
                    )
                    self.console.print(
                        f"[yellow]Task {task.task_id} rejected. "
                        f"Dependent tasks will be skipped.[/yellow]"
                    )
                    continue

            result = self._execute(task)
            self._results[task.task_id] = result
            task.status = "done" if result.success else "failed"

        self._print_summary()
        self.audit.log(
            "info",
            "session finished",
            data={"completed": len(self._results)},
        )
        return self._results

    def _deps_satisfied(self, task: Task) -> bool:
        for dep_id in task.depends_on:
            dep_result = self._results.get(dep_id)
            if dep_result is None or not dep_result.success:
                return False
        return True

    def _run_gate(self, task: Task) -> bool:
        self.audit.screenshot(self.session.page, f"gate_{task.task_id}")
        self.audit.log(
            "gate",
            f"awaiting approval for task {task.task_id}",
            task_id=task.task_id,
            data={"expected": task.expected_result, "params": task.params},
        )
        return self.approve_fn(task)

    def _execute(self, task: Task) -> ToolResult:
        self.console.print(
            f"[cyan]->[/cyan] {task.task_id}  "
            f"[dim]{task.adapter}.{task.action}[/dim]"
        )
        self.audit.log(
            "task_start",
            f"executing {task.adapter}.{task.action}",
            task_id=task.task_id,
            data={"params": task.params},
        )

        adapter = self._get_adapter(task.adapter)
        method = getattr(adapter, task.action, None)
        if method is None or not callable(method):
            result = ToolResult(
                success=False,
                action_taken=(
                    f"Attempted to call {task.adapter}.{task.action}"
                ),
                error=(
                    f"Adapter '{task.adapter}' has no method '{task.action}'"
                ),
            )
        else:
            try:
                result = method(**task.params)
                if not isinstance(result, ToolResult):
                    result = ToolResult(
                        success=False,
                        action_taken=f"{task.adapter}.{task.action} returned "
                        "non-ToolResult object",
                        error=f"Got {type(result).__name__}, expected "
                        "ToolResult",
                    )
            except Exception as e:
                result = ToolResult(
                    success=False,
                    action_taken=(
                        f"Attempted to call {task.adapter}.{task.action}"
                    ),
                    error=f"{type(e).__name__}: {e}",
                )

        self.audit.log(
            "task_end",
            result.action_taken,
            task_id=task.task_id,
            data=result.model_dump(),
        )
        status = "[green]ok[/green]" if result.success else "[red]fail[/red]"
        self.console.print(f"   {status}  {result.action_taken}")
        if result.error:
            self.console.print(f"   [red]error:[/red] {result.error}")
        return result

    def _print_summary(self) -> None:
        table = Table(title="Session summary", header_style="bold")
        table.add_column("Task", style="cyan")
        table.add_column("Adapter.Action")
        table.add_column("Status")
        table.add_column("Detail", overflow="fold")
        for task in self.task_list.tasks:
            res = self._results.get(task.task_id)
            if res is None:
                status = task.status
                detail = ""
            else:
                status = "done" if res.success else "failed"
                detail = res.error or res.action_taken
            style = {
                "done": "green",
                "failed": "red",
                "rejected": "yellow",
                "skipped": "dim",
                "pending": "dim",
            }.get(status, "white")
            table.add_row(
                task.task_id,
                f"{task.adapter}.{task.action}",
                f"[{style}]{status}[/{style}]",
                detail,
            )
        self.console.print(table)
        self.console.print(
            f"Audit log: [bold]{self.audit.log_path}[/bold]"
        )


def _cli_approve(task: Task) -> bool:
    console = Console()
    console.print(
        Panel(
            f"[bold]Approval required[/bold]\n"
            f"Task: {task.task_id}\n"
            f"Action: {task.adapter}.{task.action}\n"
            f"Params: {task.params}\n"
            f"Expected: {task.expected_result}",
            title="Human gate",
            border_style="yellow",
        )
    )
    try:
        answer = input("Approve? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
