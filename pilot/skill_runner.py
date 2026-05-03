"""Skill runner — replay a learned Skill against a live portal.

Accepts a Skill (JSON) plus a parameter dict and executes each step
through a 4-level locator fallback:

  Level 1  Exact fingerprint — test_id / id / name / aria-label
           (also tries persisted alternates from prior heals first, so
            a step that healed once stays cheap forever after)
  Level 2  Semantic — role + accessible name / text contains
  Level 3  Self-heal via pilot.agent.locator_repair.LocatorRepair —
           deterministic similarity scorer by default, LLM-pick
           when an AIClient is wired in. Returns a confidence band.
  Level 4  Human takeover — pause, screenshot, print context, wait.

After any L3 heal, a lightweight post-condition check verifies the page
state changed in response to the action. If nothing visibly changed,
the step is treated as failed and the alternate is NOT persisted —
operator pauses into L4. Heals that pass post-condition AND have
``confidence == "high"`` get their fingerprint persisted onto the step's
``alternates`` list (the runner records this on ``ToolResult.healed``;
the caller — usually pilot.agent.executor_real — writes it back to the
skill JSON).

Reuses pilot.audit.AuditLogger and pilot.models.ToolResult so replays
fold into the existing session artifact format.
"""

from __future__ import annotations

import difflib
import json
import time
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
        repair: Optional[Any] = None,
    ):
        """``repair`` is an optional pilot.agent.locator_repair.LocatorRepair
        instance. When None, the runner builds a default deterministic
        repair on first use (no LLM). Pass an LLM-backed repair when
        you want self-heal to use the model."""
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
        self._repair = repair  # lazily built in _resolve_locator if None

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

        # Allow framework state + effects to settle between steps, and
        # flush any pending async work. Plus: wait for known
        # in-progress indicators ("Saving...", "Applying...", spinners)
        # to disappear before we act on the next step. Catches the most
        # common race where a recorded "click Apply" fires while the
        # previous Save is still committing on the server.
        self._wait_for_page_settle()

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
        # Navigate with networkidle: waits for both DOMContentLoaded
        # and a 500ms quiet network window. SPAs that fetch data after
        # initial load (most enterprise portals) need this signal —
        # plain domcontentloaded fires before the data is rendered, so
        # the next step's locator misses.
        try:
            self.session.page.goto(url, wait_until="networkidle", timeout=15000)
        except PWTimeoutError:
            # Fall back to domcontentloaded if the page never reaches
            # networkidle (some portals keep long-poll connections open).
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
        locator, level, heal = self._resolve_locator(step)
        if locator is None:
            return self._fallback_human(step, "could not locate click target")
        page = self.session.page
        verified = self._execute_with_heal_check(
            page, level, heal, lambda: locator.click(timeout=4000)
        )
        shot = self._screenshot(f"step_{step.index}_click")
        return self._build_action_result(
            success=verified,
            level=level,
            heal=heal,
            action_taken=f"clicked {step.semantic_label} [{LEVEL_LABELS[level]}]",
            screenshot_path=shot,
            unverified_error="L3 heal: page state did not change after click",
        )

    def _do_change(
        self, step: SkillStep, value: Optional[str]
    ) -> tuple[ToolResult, int]:
        locator, level, heal = self._resolve_locator(step)
        if locator is None:
            return self._fallback_human(step, "could not locate input target")
        fp = step.fingerprint
        tag = (fp.tag if fp else "") or ""
        input_type = (fp.input_type if fp else None) or ""
        page = self.session.page
        if tag == "select":
            verified = self._execute_with_heal_check(
                page,
                level,
                heal,
                lambda: self._select_option_with_fuzzy_fallback(locator, value or ""),
            )
        elif input_type in ("checkbox", "radio"):
            # Boolean inputs — Playwright's set_checked is the right API,
            # not fill(). Coerce common truthy strings to bool. Recording
            # captures value="true"/"false" via the grabber's change handler.
            target_checked = (value or "").strip().lower() in (
                "true", "1", "on", "yes", "checked"
            )
            verified = self._execute_with_heal_check(
                page,
                level,
                heal,
                lambda: locator.set_checked(target_checked),
            )
        else:
            verified = self._execute_with_heal_check(
                page, level, heal, lambda: locator.fill(value or "")
            )
        shot = self._screenshot(f"step_{step.index}_change")
        return self._build_action_result(
            success=verified,
            level=level,
            heal=heal,
            action_taken=f"set {step.semantic_label} = {value!r} [{LEVEL_LABELS[level]}]",
            screenshot_path=shot,
            unverified_error="L3 heal: page state did not change after fill",
        )

    def _do_upload(
        self, step: SkillStep, value: Optional[str]
    ) -> tuple[ToolResult, int]:
        locator, level, heal = self._resolve_locator(step)
        if locator is None:
            return self._fallback_human(step, "could not locate file input")
        if not value:
            return (
                ToolResult(
                    success=False,
                    action_taken="upload",
                    error="no file_path parameter resolved",
                    healed=heal,
                ),
                level,
            )
        page = self.session.page
        verified = self._execute_with_heal_check(
            page, level, heal, lambda: locator.set_input_files(value)
        )
        shot = self._screenshot(f"step_{step.index}_upload")
        return self._build_action_result(
            success=verified,
            level=level,
            heal=heal,
            action_taken=f"uploaded {value}",
            screenshot_path=shot,
            unverified_error="L3 heal: page state did not change after upload",
        )

    def _do_key(
        self, step: SkillStep, value: Optional[str]
    ) -> tuple[ToolResult, int]:
        locator, level, heal = self._resolve_locator(step)
        if locator is None:
            return self._fallback_human(step, "could not locate key target")
        key = value or step.value or "Enter"
        page = self.session.page
        verified = self._execute_with_heal_check(
            page, level, heal, lambda: locator.press(key)
        )
        return self._build_action_result(
            success=verified,
            level=level,
            heal=heal,
            action_taken=f"pressed {key!r}",
            screenshot_path=None,
            unverified_error="L3 heal: page state did not change after key press",
        )

    def _build_action_result(
        self,
        *,
        success: bool,
        level: int,
        heal: Optional[dict[str, Any]],
        action_taken: str,
        screenshot_path: Optional[str],
        unverified_error: str,
    ) -> tuple[ToolResult, int]:
        if success:
            return (
                ToolResult(
                    success=True,
                    action_taken=action_taken,
                    screenshot_path=screenshot_path,
                    healed=heal,
                ),
                level,
            )
        # Healed action ran but post-condition didn't pass — escalate as
        # a step failure. The orchestrator's pause/retry/skip flow takes
        # over from here. The healed dict is preserved so the operator
        # can see what was attempted.
        return (
            ToolResult(
                success=False,
                action_taken=action_taken,
                error=unverified_error,
                screenshot_path=screenshot_path,
                healed=heal,
            ),
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
    ) -> tuple[Optional[Locator], int, Optional[dict[str, Any]]]:
        """Resolve a step's locator. Returns (locator, level, heal_info).

        ``heal_info`` is None for L1/L2/L4. For L3 it carries a dict
        the action method enriches with post_condition_passed before
        attaching to the resulting ToolResult.

        If the fingerprint has ``templates`` (e.g. ``test_id`` was
        recorded as ``"row-A-9001"`` and templated as ``"row-{content_id}"``),
        we first materialize a fresh fingerprint with the current
        parameter values substituted in. The materialized fingerprint
        is what L1 / L2 / L3 see — so a re-templated DOM id like
        ``row-B-12345`` resolves at L1 instead of falling through to
        L3 self-heal.
        """
        page = self.session.page
        fp = step.fingerprint
        if fp is None:
            return None, 4, None
        fp = self._materialize_fingerprint(fp)

        # --- Level 1 — exact stable attributes ----
        # Try the original fingerprint first, then any alternates persisted
        # by prior heals so a step that healed once is cheap forever after.
        for candidate in self._fp_with_alternates(fp):
            l1 = self._level1(page, candidate)
            if l1 is not None:
                return l1, 1, None

        # --- Level 2 — semantic ----
        for candidate in self._fp_with_alternates(fp):
            l2 = self._level2(page, candidate)
            if l2 is not None:
                return l2, 2, None

        # --- Level 3 — self-heal via locator_repair ----
        return self._level3(page, fp, step.semantic_label)

    def _select_option_with_fuzzy_fallback(
        self, locator: Locator, value: str
    ) -> None:
        """Pick a select's option, falling back from exact value-match
        to fuzzy text-match if the recorded value is no longer one of
        the dropdown's option values.

        Real-world driver: state-dependent dropdowns (e.g. asset status
        changes the available transition list — recorded value
        ``QC_IN_PROGRESS`` may exist on one run but not on another
        with a different starting state). When that happens, we look
        for an option whose visible text matches the recorded value
        instead, with similarity >= 0.7 to avoid wild guesses.

        Raises an Exception with ``error_kind=option_not_available``-
        style context if no match clears the threshold; the calling
        action method converts it to a structured ToolResult failure.
        """
        try:
            locator.select_option(value=value, timeout=2000)
            return
        except Exception:
            pass
        try:
            locator.select_option(label=value, timeout=2000)
            return
        except Exception:
            pass

        # Last resort: read the actual options off the rendered DOM
        # and pick the closest text match. Works for the QC-style
        # state-dependent option lists where the recorded VALUE is no
        # longer valid but a similar-meaning option does exist.
        try:
            opts = locator.evaluate(
                "el => Array.from(el.options).map(o => "
                "({ value: o.value, text: (o.textContent || '').trim() }))"
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"option_not_available: '{value}' is not a valid option "
                f"and the option list could not be read ({e})"
            ) from e

        if not opts:
            raise RuntimeError(
                f"option_not_available: select element has no options"
            )

        best_score = 0.0
        best_value = None
        target = value.lower()
        for o in opts:
            txt = (o.get("text") or "").lower()
            val = (o.get("value") or "").lower()
            score = max(
                difflib.SequenceMatcher(None, target, txt).ratio(),
                difflib.SequenceMatcher(None, target, val).ratio(),
            )
            if score > best_score:
                best_score = score
                best_value = o.get("value")

        if best_score < 0.7 or not best_value:
            available_texts = ", ".join(
                (o.get("text") or "").strip() for o in opts[:8]
            )
            raise RuntimeError(
                f"option_not_available: no option matches {value!r} "
                f"(closest score={best_score:.2f}). "
                f"Available: [{available_texts}]"
            )
        self.audit.log(
            "info",
            (
                f"select fuzzy-matched: recorded={value!r} -> "
                f"chose value={best_value!r} (similarity={best_score:.2f})"
            ),
        )
        locator.select_option(value=best_value)

    def _materialize_fingerprint(
        self, fp: ElementFingerprint
    ) -> ElementFingerprint:
        """If the fingerprint has templates, build a copy with the
        current parameter values substituted into the templated fields.
        Returns the original fingerprint if no templates apply.
        """
        if not fp.templates:
            return fp
        # model_copy with deep update so we don't mutate the skill in
        # memory — the original templates stay intact for the next
        # parameter set.
        materialized = fp.model_copy(deep=True)
        any_change = False
        for field_name, template in fp.templates.items():
            try:
                resolved = template.format(**self.params)
            except KeyError:
                # Template references a param not provided this run —
                # leave the literal value in place (L2/L3 will likely
                # need to handle it).
                continue
            current = getattr(materialized, field_name, None)
            if resolved != current:
                setattr(materialized, field_name, resolved)
                any_change = True
        return materialized if any_change else fp

    def _fp_with_alternates(self, fp: ElementFingerprint):
        """Yield the original fingerprint, then each persisted alternate."""
        yield fp
        for alt in fp.alternates or []:
            yield alt

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

    def _level3(
        self,
        page: Page,
        fp: ElementFingerprint,
        semantic_label: Optional[str],
    ) -> tuple[Optional[Locator], int, Optional[dict[str, Any]]]:
        """Self-heal via pilot.agent.locator_repair.

        Returns (locator, 3, heal_info) on a confident pick. Returns
        (None, 4, None) if the repair refuses or fails — caller will
        escalate to human takeover.
        """
        repair = self._get_repair()
        result = repair.heal(page, fp, semantic_label)
        if result.locator is None or result.confidence == "low":
            self.audit.log(
                "info",
                f"L3 refused: confidence={result.confidence} reason={result.reason}",
            )
            return None, 4, None

        new_fp_dump = (
            result.new_fingerprint.model_dump() if result.new_fingerprint else None
        )
        new_summary = "?"
        if result.new_fingerprint:
            new_summary = (
                result.new_fingerprint.test_id
                or result.new_fingerprint.accessible_name
                or (result.new_fingerprint.text or "")[:60]
                or result.new_fingerprint.tag
                or "?"
            )
        original_summary = (
            fp.test_id or fp.accessible_name or (fp.text or "")[:60] or fp.tag or "?"
        )
        heal_info: dict[str, Any] = {
            "original_summary": original_summary,
            "new_summary": new_summary,
            "confidence": result.confidence,
            "reason": result.reason,
            "post_condition_passed": False,  # filled by action method
            "new_fingerprint": new_fp_dump,
            "backend": result.backend,
        }
        self.audit.log(
            "info",
            (
                f"L3 healed: {original_summary!r} -> {new_summary!r} "
                f"({result.confidence}, {result.backend})"
            ),
        )
        return result.locator, 3, heal_info

    def _get_repair(self):
        """Lazily build a deterministic LocatorRepair if none was injected.

        Imported at call-site to avoid a circular import with pilot.agent.
        """
        if self._repair is not None:
            return self._repair
        from pilot.agent.locator_repair import LocatorRepair

        self._repair = LocatorRepair()  # deterministic, no LLM
        return self._repair

    # ---- Heal post-condition ---------------------------------------------

    # Common in-progress indicator patterns. Portals signal mid-action
    # state via testids like ``status-saving``, ``status-applying``,
    # ``loading-...``, ``spinner-...``, role=progressbar. We wait for
    # any of these to clear before proceeding. This is a heuristic —
    # if a portal uses different conventions, the wait is a no-op
    # (returns immediately) and the existing 4-level fallback handles
    # the failure case.
    _SPINNER_SELECTOR = (
        "[data-testid^='status-saving'],"
        "[data-testid^='status-applying'],"
        "[data-testid^='loading-'],"
        "[data-testid^='spinner-'],"
        "[data-testid='loading'],"
        "[data-testid='spinner'],"
        "[role='progressbar']"
    )

    def _wait_for_page_settle(self, max_ms: int = 4000) -> None:
        """Wait briefly for the page to be ready for the next action.

        Two-stage: short fixed pause (250ms) for synchronous state
        propagation, then a bounded wait for any known in-progress
        indicators to clear. Total bounded by ``max_ms``.
        """
        page = self.session.page
        try:
            page.wait_for_timeout(250)
        except Exception:
            return
        try:
            spinner = page.locator(self._SPINNER_SELECTOR)
            if spinner.count() > 0:
                # Wait for the spinner to disappear; if it doesn't,
                # we just proceed and let the action's own auto-wait
                # handle the fallout.
                try:
                    spinner.first.wait_for(state="hidden", timeout=max_ms)
                except Exception:
                    pass
        except Exception:
            pass

    def _page_state_signature(self, page: Page) -> tuple[str, int, int]:
        """Cheap whole-page signature: (url, body innerText length, count
        of visible interactables). Used to verify a healed action
        actually changed something. Not a strong assertion — but a
        clicked button that does nothing leaves the signature unchanged,
        which catches the most obvious wrong-pick failure mode."""
        try:
            sig = page.evaluate(
                "() => { const txt = document.body ? document.body.innerText : '';"
                " const interactables = document.querySelectorAll("
                "'button:not([disabled]), a[href], input:not([disabled]),"
                " select:not([disabled]), textarea:not([disabled]),"
                " [role=\"button\"]'"
                " ).length;"
                " return { url: location.href,"
                " textLen: txt.length,"
                " interactables: interactables }; }"
            )
            return (
                str(sig.get("url", "")),
                int(sig.get("textLen", 0)),
                int(sig.get("interactables", 0)),
            )
        except Exception:
            return ("", 0, 0)

    def _execute_with_heal_check(
        self,
        page: Page,
        level: int,
        heal_info: Optional[dict[str, Any]],
        action_callable: Callable[[], None],
    ) -> bool:
        """Run an action; if it was a healed (L3) action, check that
        the page state changed afterward. Returns True if the action
        ran cleanly. Mutates heal_info['post_condition_passed'].
        """
        if level != 3 or heal_info is None:
            action_callable()
            return True
        before = self._page_state_signature(page)
        action_callable()
        # SPA settle window — give effects time to render
        try:
            page.wait_for_timeout(350)
        except Exception:
            pass
        after = self._page_state_signature(page)
        passed = before != after
        heal_info["post_condition_passed"] = passed
        return passed

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
