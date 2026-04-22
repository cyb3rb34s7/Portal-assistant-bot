"""Skill runner — replay a learned Skill against a live portal.

Accepts a Skill (JSON) plus a parameter dict and executes each step
through a 4-level locator fallback:

  Level 1  Exact fingerprint — test_id / id / name / aria-label
  Level 2  Semantic — role + accessible name / text contains
  Level 3  Deterministic best-match using the full fingerprint as a
           similarity query against interactable elements on the page.
           (No LLM dependency in POC; ready to be swapped for AI later.)
  Level 4  Human takeover — pause, screenshot, print context, wait.

Reuses pilot.audit.AuditLogger and pilot.models.ToolResult so replays
fold into the existing session artifact format.
"""

from __future__ import annotations

import difflib
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeoutError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from .audit import AuditLogger
from .browser import BrowserSession, connect_to_chrome
from .models import ToolResult
from .skill_models import ElementFingerprint, ParamBinding, Skill, SkillStep


LEVEL_LABELS = {
    1: "L1 exact",
    2: "L2 semantic",
    3: "L3 fingerprint-match",
    4: "L4 human",
}


class SkillExecutionError(Exception):
    pass


class SkillRunner:
    def __init__(
        self,
        session: BrowserSession,
        skill: Skill,
        params: dict[str, Any],
        sessions_dir: Path,
        base_url: Optional[str] = None,
        approve_fn: Optional[Callable[[SkillStep], bool]] = None,
        takeover_fn: Optional[Callable[[SkillStep], bool]] = None,
        console: Optional[Console] = None,
    ):
        self.session = session
        self.skill = skill
        self.params = params
        self.base_url = base_url or skill.base_url or ""
        self.session_id = uuid.uuid4().hex[:12]
        self.audit = AuditLogger(self.session_id, sessions_dir)
        self.console = console or Console()
        self.approve_fn = approve_fn or _cli_approve
        self.takeover_fn = takeover_fn or _cli_takeover
        self._results: list[tuple[SkillStep, ToolResult, int]] = []

    # ---- Public --------------------------------------------------------

    def run(self) -> list[tuple[SkillStep, ToolResult, int]]:
        self.console.print(
            Panel.fit(
                f"[bold]Replay skill[/bold] {self.skill.name}\n"
                f"Session: {self.session_id}\n"
                f"Steps:   {len(self.skill.steps)}\n"
                f"Params:  {self.params}",
                border_style="magenta",
            )
        )
        self.audit.log(
            "info",
            "replay started",
            data={"skill": self.skill.name, "params": self.params},
        )

        for step in self.skill.steps:
            if step.requires_gate:
                approved = self.approve_fn(step)
                self.audit.log(
                    "gate",
                    f"gate {'approved' if approved else 'rejected'} for step {step.index}",
                    data={"label": step.semantic_label},
                )
                if not approved:
                    result = ToolResult(
                        success=False,
                        action_taken=f"Rejected at gate: {step.semantic_label}",
                    )
                    self._results.append((step, result, 0))
                    continue

            result, level = self._execute_step(step)
            self._results.append((step, result, level))
            if not result.success:
                self.audit.log(
                    "error",
                    f"step {step.index} failed",
                    data={"error": result.error, "label": step.semantic_label},
                )
                # On failure, still continue — the runner logs and keeps going
                # unless the caller wants hard stop. Hard stop is configurable
                # via subclass / future flag.

        self._summary()
        self.audit.log("info", "replay finished")
        return self._results

    # ---- Execution -----------------------------------------------------

    def _execute_step(self, step: SkillStep) -> tuple[ToolResult, int]:
        self.console.print(
            f"[cyan]-[/cyan] step {step.index:02d}  "
            f"[dim]{step.action}[/dim]  "
            f"[white]{step.semantic_label or ''}[/white]"
        )
        self.audit.log(
            "task_start",
            f"step {step.index} {step.action}",
            data={"label": step.semantic_label},
        )

        value = self._resolved_value(step)

        # Allow React/Vue/Angular state + effects to settle between steps,
        # and flush any pending async work. Cheap insurance against races.
        try:
            self.session.page.wait_for_timeout(80)
        except Exception:
            pass

        try:
            if step.action == "navigate":
                return self._do_navigate(step)
            if step.action == "submit":
                # A form submit fires implicitly when the submit button is
                # clicked. Our traces always include a click on the submit
                # button immediately before the submit event, so replaying
                # submit as an extra click tends to mis-target. Treat as
                # implicit success.
                return (
                    ToolResult(
                        success=True,
                        action_taken=f"submit (implicit after click)",
                    ),
                    1,
                )
            if step.action == "click":
                return self._do_click(step)
            if step.action == "change":
                return self._do_change(step, value)
            if step.action == "upload":
                return self._do_upload(step, value)
            if step.action == "key":
                return self._do_key(step, value)
            if step.action == "wait":
                return self._do_wait(step)
            return (
                ToolResult(
                    success=False,
                    action_taken=f"Unknown action {step.action}",
                    error="unsupported action",
                ),
                0,
            )
        except Exception as e:
            shot = self._screenshot(f"step_{step.index}_error")
            return (
                ToolResult(
                    success=False,
                    action_taken=f"step {step.index} {step.action}",
                    error=f"{type(e).__name__}: {e}",
                    screenshot_path=shot,
                ),
                0,
            )

    def _do_navigate(self, step: SkillStep) -> tuple[ToolResult, int]:
        url = step.url or self.base_url
        if not url:
            return (
                ToolResult(
                    success=False,
                    action_taken="navigate",
                    error="no URL to navigate to",
                ),
                0,
            )
        self.session.page.goto(url, wait_until="domcontentloaded")
        shot = self._screenshot(f"step_{step.index}_navigate")
        return (
            ToolResult(
                success=True,
                action_taken=f"navigated to {url}",
                screenshot_path=shot,
            ),
            1,
        )

    def _do_click(self, step: SkillStep) -> tuple[ToolResult, int]:
        locator, level = self._resolve_locator(step)
        if locator is None:
            return self._fallback_human(step, "could not locate click target")
        locator.click(timeout=4000)
        shot = self._screenshot(f"step_{step.index}_click")
        return (
            ToolResult(
                success=True,
                action_taken=f"clicked {step.semantic_label} [{LEVEL_LABELS[level]}]",
                screenshot_path=shot,
            ),
            level,
        )

    def _do_change(
        self, step: SkillStep, value: Optional[str]
    ) -> tuple[ToolResult, int]:
        locator, level = self._resolve_locator(step)
        if locator is None:
            return self._fallback_human(step, "could not locate input target")
        fp = step.fingerprint
        tag = (fp.tag if fp else "") or ""
        if tag == "select":
            locator.select_option(value=value or "")
        else:
            locator.fill(value or "")
        shot = self._screenshot(f"step_{step.index}_change")
        return (
            ToolResult(
                success=True,
                action_taken=f"set {step.semantic_label} = {value!r} [{LEVEL_LABELS[level]}]",
                screenshot_path=shot,
            ),
            level,
        )

    def _do_upload(
        self, step: SkillStep, value: Optional[str]
    ) -> tuple[ToolResult, int]:
        locator, level = self._resolve_locator(step)
        if locator is None:
            return self._fallback_human(step, "could not locate file input")
        if not value:
            return (
                ToolResult(
                    success=False,
                    action_taken="upload",
                    error="no file_path parameter resolved",
                ),
                level,
            )
        locator.set_input_files(value)
        shot = self._screenshot(f"step_{step.index}_upload")
        return (
            ToolResult(
                success=True,
                action_taken=f"uploaded {value}",
                screenshot_path=shot,
            ),
            level,
        )

    def _do_key(
        self, step: SkillStep, value: Optional[str]
    ) -> tuple[ToolResult, int]:
        locator, level = self._resolve_locator(step)
        if locator is None:
            return self._fallback_human(step, "could not locate key target")
        locator.press(value or step.value or "Enter")
        return (
            ToolResult(success=True, action_taken=f"pressed {value!r}"),
            level,
        )

    def _do_wait(self, step: SkillStep) -> tuple[ToolResult, int]:
        self.session.page.wait_for_timeout(step.wait_ms or 500)
        return (
            ToolResult(success=True, action_taken=f"waited {step.wait_ms}ms"),
            1,
        )

    # ---- Parameter resolution ----------------------------------------------

    def _resolved_value(self, step: SkillStep) -> Optional[str]:
        binding = step.param_binding
        if binding is None:
            # literal
            return step.value if step.value is not None else step.file_path
        provided = self.params.get(binding.name)
        if provided is None:
            raise SkillExecutionError(
                f"Step {step.index} needs parameter '{binding.name}' but it was not provided"
            )
        if binding.mode == "template" and binding.template:
            return binding.template.format(**self.params)
        return str(provided)

    # ---- Locator resolution with 4-level fallback --------------------------

    def _resolve_locator(
        self, step: SkillStep
    ) -> tuple[Optional[Locator], int]:
        page = self.session.page
        fp = step.fingerprint
        if fp is None:
            return None, 4

        # --- Level 1 — exact stable attributes ----
        l1 = self._level1(page, fp)
        if l1 is not None:
            return l1, 1

        # --- Level 2 — semantic ----
        l2 = self._level2(page, fp)
        if l2 is not None:
            return l2, 2

        # --- Level 3 — deterministic fingerprint match across interactables ----
        l3 = self._level3(page, fp)
        if l3 is not None:
            return l3, 3

        return None, 4

    def _level1(self, page: Page, fp: ElementFingerprint) -> Optional[Locator]:
        if fp.test_id:
            loc = page.get_by_test_id(fp.test_id)
            if _first_visible(loc):
                return loc.first
        if fp.element_id:
            loc = page.locator(f"#{_css_escape(fp.element_id)}")
            if _first_visible(loc):
                return loc.first
        if fp.name:
            loc = page.locator(f"[name='{fp.name}']")
            if _first_visible(loc):
                return loc.first
        if fp.aria_label:
            loc = page.get_by_label(fp.aria_label, exact=False)
            if _first_visible(loc):
                return loc.first
        return None

    def _level2(self, page: Page, fp: ElementFingerprint) -> Optional[Locator]:
        if fp.role and fp.accessible_name:
            try:
                loc = page.get_by_role(fp.role, name=fp.accessible_name, exact=False)
                if _first_visible(loc):
                    return loc.first
            except Exception:
                pass
        if fp.placeholder:
            loc = page.get_by_placeholder(fp.placeholder, exact=False)
            if _first_visible(loc):
                return loc.first
        if fp.accessible_name:
            try:
                loc = page.get_by_text(fp.accessible_name, exact=False)
                if _first_visible(loc):
                    return loc.first
            except Exception:
                pass
        if fp.text and len(fp.text) >= 3:
            text = fp.text.strip()[:50]
            try:
                loc = page.get_by_text(text, exact=False)
                if _first_visible(loc):
                    return loc.first
            except Exception:
                pass
        if fp.css_path:
            try:
                loc = page.locator(fp.css_path)
                if _first_visible(loc):
                    return loc.first
            except Exception:
                pass
        return None

    def _level3(self, page: Page, fp: ElementFingerprint) -> Optional[Locator]:
        """Deterministic best-match against interactable elements on the page.

        Queries all interactable candidates, scores each against the
        fingerprint, and returns a locator for the top match if the
        score clears a confidence threshold.
        """
        try:
            candidates = page.evaluate(_JS_LIST_INTERACTABLES)
        except Exception:
            return None
        if not candidates:
            return None

        best_score = 0.0
        best_xpath: Optional[str] = None
        for c in candidates:
            score = _similarity(fp, c)
            if score > best_score:
                best_score = score
                best_xpath = c.get("xpath")

        if best_score >= 0.55 and best_xpath:
            try:
                loc = page.locator(f"xpath={best_xpath}")
                if _first_visible(loc):
                    self.audit.log(
                        "info",
                        f"L3 match score={best_score:.2f} xpath={best_xpath}",
                    )
                    return loc.first
            except Exception:
                return None
        return None

    # ---- Human takeover ----------------------------------------------------

    def _fallback_human(
        self, step: SkillStep, reason: str
    ) -> tuple[ToolResult, int]:
        shot = self._screenshot(f"step_{step.index}_takeover")
        self.audit.log(
            "gate",
            f"takeover required for step {step.index}: {reason}",
            data={"label": step.semantic_label},
        )
        approved = self.takeover_fn(step)
        if approved:
            return (
                ToolResult(
                    success=True,
                    action_taken=f"human completed step {step.index}",
                    screenshot_path=shot,
                ),
                4,
            )
        return (
            ToolResult(
                success=False,
                action_taken=f"step {step.index} abandoned",
                error=reason,
                screenshot_path=shot,
            ),
            4,
        )

    # ---- Utilities -----------------------------------------------------

    def _screenshot(self, label: str) -> Optional[str]:
        try:
            return self.audit.screenshot(self.session.page, label) or None
        except Exception:
            return None

    def _summary(self) -> None:
        table = Table(title="Replay summary", header_style="bold")
        table.add_column("#", style="dim")
        table.add_column("Action", style="cyan")
        table.add_column("Label")
        table.add_column("Level")
        table.add_column("Status")
        table.add_column("Detail", overflow="fold")
        for step, result, level in self._results:
            status = "[green]ok[/green]" if result.success else "[red]fail[/red]"
            detail = result.error or result.action_taken
            table.add_row(
                str(step.index),
                step.action,
                step.semantic_label or "",
                LEVEL_LABELS.get(level, ""),
                status,
                detail or "",
            )
        self.console.print(table)
        self.console.print(f"Audit log: [bold]{self.audit.log_path}[/bold]")


# ---- Helpers --------------------------------------------------------------


def _css_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _first_visible(loc: Locator) -> bool:
    try:
        if loc.count() == 0:
            return False
        return loc.first.is_visible(timeout=500)
    except (PWTimeoutError, Exception):
        try:
            return loc.count() > 0
        except Exception:
            return False


def _similarity(fp: ElementFingerprint, cand: dict[str, Any]) -> float:
    score = 0.0
    weights_total = 0.0

    def add(weight: float, matcher: float) -> None:
        nonlocal score, weights_total
        score += weight * matcher
        weights_total += weight

    if fp.test_id and cand.get("testId"):
        add(3.0, 1.0 if fp.test_id == cand["testId"] else 0.0)
    if fp.element_id and cand.get("id"):
        add(2.0, 1.0 if fp.element_id == cand["id"] else 0.0)
    if fp.role and cand.get("role"):
        add(1.5, 1.0 if fp.role == cand["role"] else 0.0)
    if fp.tag and cand.get("tag"):
        add(0.5, 1.0 if fp.tag == cand["tag"] else 0.0)
    if fp.accessible_name and cand.get("accessibleName"):
        add(
            2.5,
            difflib.SequenceMatcher(
                None,
                fp.accessible_name.lower(),
                (cand["accessibleName"] or "").lower(),
            ).ratio(),
        )
    if fp.text and cand.get("text"):
        add(
            1.0,
            difflib.SequenceMatcher(
                None,
                fp.text.lower()[:60],
                (cand["text"] or "").lower()[:60],
            ).ratio(),
        )
    if fp.placeholder and cand.get("placeholder"):
        add(1.5, 1.0 if fp.placeholder == cand.get("placeholder") else 0.0)
    if fp.landmark and cand.get("landmark"):
        add(
            1.0,
            1.0 if fp.landmark == cand["landmark"] else 0.0,
        )

    if weights_total == 0:
        return 0.0
    return score / weights_total


_JS_LIST_INTERACTABLES = """
(() => {
  const out = [];
  const selector = 'button, a, input, select, textarea, [role], [data-testid], [onclick]';
  const nodes = Array.from(document.querySelectorAll(selector));
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const pathParts = [];
    let node = el;
    while (node && node.nodeType === 1 && pathParts.length < 10) {
      let seg = node.nodeName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sameTag = Array.from(parent.children).filter(
          (c) => c.nodeName === node.nodeName
        );
        if (sameTag.length > 1) seg += '[' + (sameTag.indexOf(node) + 1) + ']';
      }
      pathParts.unshift(seg);
      node = parent;
    }
    const xpath = '/' + pathParts.join('/');
    let landmark = null;
    let cur = el.parentElement;
    while (cur) {
      const tid = cur.getAttribute && cur.getAttribute('data-testid');
      if (tid && /(page|panel|modal|dialog)/i.test(tid)) { landmark = tid; break; }
      cur = cur.parentElement;
    }
    out.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      testId: el.getAttribute('data-testid'),
      role: el.getAttribute('role') || null,
      accessibleName: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 120),
      text: (el.innerText || el.textContent || '').trim().slice(0, 120),
      placeholder: el.getAttribute('placeholder'),
      landmark,
      xpath,
    });
  }
  return out;
})()
"""


def _cli_approve(step: SkillStep) -> bool:
    console = Console()
    console.print(
        Panel(
            f"[bold]Approval required[/bold]\n"
            f"Step: {step.index}  {step.action}\n"
            f"Label: {step.semantic_label}\n"
            f"Reason: {step.gate_reason or 'marked irreversible during annotation'}",
            title="Human gate",
            border_style="yellow",
        )
    )
    try:
        return Confirm.ask("Approve?", default=False)
    except EOFError:
        return False


def _cli_takeover(step: SkillStep) -> bool:
    console = Console()
    console.print(
        Panel(
            f"[bold]Manual takeover needed[/bold]\n"
            f"Step: {step.index}  {step.action}\n"
            f"Label: {step.semantic_label}\n"
            f"Complete this step in the browser, then press Enter.",
            title="Takeover",
            border_style="red",
        )
    )
    try:
        input("Press Enter when done (or Ctrl+C to abort)... ")
        return True
    except (EOFError, KeyboardInterrupt):
        return False


# ---- CLI ------------------------------------------------------------------


def run_skill_from_file(
    skill_path: Path,
    params: dict[str, Any],
    base_url: str = "http://localhost:5173",
    cdp: str = "http://localhost:9222",
    sessions_dir: Path = Path("sessions"),
) -> list[tuple[SkillStep, ToolResult, int]]:
    console = Console()
    if not skill_path.exists():
        console.print(f"[red]Skill file not found:[/red] {skill_path}")
        raise SystemExit(2)

    skill_dict = json.loads(skill_path.read_text(encoding="utf-8"))
    skill = Skill.model_validate(skill_dict)

    console.print(f"Connecting to Chrome at [bold]{cdp}[/bold] ...")
    session = connect_to_chrome(cdp, target_url_substring=base_url)
    try:
        runner = SkillRunner(
            session=session,
            skill=skill,
            params=params,
            sessions_dir=sessions_dir,
            base_url=base_url,
        )
        return runner.run()
    finally:
        session.close()
