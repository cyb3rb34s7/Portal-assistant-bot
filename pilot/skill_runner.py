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
from .skill_models import (
    ElementFingerprint,
    ExpectedSignals,
    ParamBinding,
    Skill,
    SkillStep,
    StepAssertion,
)


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
        sub_step_overrides: Optional[dict[int, dict[str, Any]]] = None,
    ):
        """``repair`` is an optional pilot.agent.locator_repair.LocatorRepair
        instance. When None, the runner builds a default deterministic
        repair on first use (no LLM). Pass an LLM-backed repair when
        you want self-heal to use the model.

        ``sub_step_overrides`` is keyed by sub-step ``index`` and carries
        an override locator (e.g. ``{12: {"test_id": "search-row-A-9002"}}``).
        When a step's index matches, the override replaces the materialized
        fingerprint's identifying fields BEFORE L1 resolution -- so the
        operator's disambiguation pick from a previous failed run goes
        straight through as an L1 hit instead of fighting the templated
        testid that produced ambiguity in the first place. Only honored
        once per step: after the action runs, the override is consumed.
        """
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
        self._sub_step_overrides = dict(sub_step_overrides or {})
        # Portal-level URL ignore list, set by the caller (the agent
        # executor reads it off PortalContext.network_ignore). Filters
        # noisy long-poll / notifications channels out of the in-flight
        # wait predicate.
        self.portal_network_ignore: list[str] = []
        self.network_quiet_ms: int = 250
        # Disambiguation hints keyed by step.index. Carries operator
        # picks from prior runs so we can resolve ambiguity without
        # pausing again. Set externally by the executor reading the
        # skill's .hints.json sidecar.
        self.disambiguation_hints: dict[int, dict[str, Any]] = {}
        # Set by _resolve_locator when L1 finds >1 match for a templated
        # locator. Action methods read it to emit ambiguous_target
        # instead of falling through to generic L4 takeover. Cleared on
        # every step entry.
        self._pending_ambiguity: list[dict[str, Any]] | None = None

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
            # Finalize + emit the diagnostic. final_level + error_kind get
            # filled here because they're only known after the action ran.
            diag = getattr(self, "_diag", None)
            if diag is not None:
                diag["final_level"] = level
                diag["success"] = bool(result.success)
                if not result.success:
                    diag["error_kind"] = result.error_kind or "step_failed"
                self.audit.log("step_diagnostic", "", data=diag)
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

        # Reset per-step state -- ambiguity flag from a previous step
        # must never leak into the current one.
        self._pending_ambiguity = None

        # Set the idempotency context for any destructive API calls
        # this step triggers. Retries of the same step within the run
        # will reuse the same key, so the backend dedupes.
        self._set_step_idem_context(self.session.page, step)

        # Per-step diagnostic record. Built up by the L1/L2/L3 paths and
        # the wait helpers, then emitted as one structured audit entry
        # at the end of the step so /api/sessions/<id>/diagnostics can
        # surface "which level resolved this step, did it heal, how long
        # did each wait take" without parsing free-text log lines.
        self._diag = {
            "step_index": step.index,
            "action": step.action,
            "label": step.semantic_label,
            "levels_attempted": [],  # filled by _level1/2/3
            "final_level": None,  # 1/2/3/4 or 0 (ambiguous)
            "ambiguity_candidate_count": 0,
            "waits_ms": {"network": 0, "dom": 0, "spinner": 0},
            "post_condition_passed": None,
            "heal_backend": None,
            "heal_confidence": None,
            "used_operator_override": False,
            "error_kind": None,
        }

        value = self._resolved_value(step)

        # Allow framework state + effects to settle between steps, and
        # flush any pending async work. When step.expected_signals is
        # declared, wait specifically for those targets (network URL
        # patterns + DOM conditions). Otherwise fall back to the
        # generic in-flight + DOM-quiescence heuristic.
        self._wait_for_page_settle(expected=step.expected_signals)

        try:
            if step.action == "navigate":
                result, level = self._do_navigate(step)
            elif step.action == "submit":
                # A form submit fires implicitly when the submit button is
                # clicked. Our traces always include a click on the submit
                # button immediately before the submit event, so replaying
                # submit as an extra click tends to mis-target. Treat as
                # implicit success.
                result, level = (
                    ToolResult(
                        success=True,
                        action_taken="submit (implicit after click)",
                    ),
                    1,
                )
            elif step.action == "click":
                result, level = self._do_click(step)
            elif step.action == "change":
                result, level = self._do_change(step, value)
            elif step.action == "upload":
                result, level = self._do_upload(step, value)
            elif step.action == "key":
                result, level = self._do_key(step, value)
            elif step.action == "wait":
                result, level = self._do_wait(step)
            elif step.action == "set_selection":
                result, level = self._do_set_selection(step)
            else:
                return (
                    ToolResult(
                        success=False,
                        action_taken=f"Unknown action {step.action}",
                        error="unsupported action",
                    ),
                    0,
                )

            # Declarative post-condition check. A "successful" action
            # whose post-condition fails is reclassified as failed with
            # error_kind="post_condition_failed" so the orchestrator's
            # pause/heal flow can recover.
            if result.success and step.assert_after:
                ok, desc = self._verify_assertions(step)
                if not ok:
                    shot = self._screenshot(f"step_{step.index}_post_cond_fail")
                    return (
                        ToolResult(
                            success=False,
                            action_taken=result.action_taken,
                            error=f"post-condition failed: {desc}",
                            error_kind="post_condition_failed",
                            error_details={"assertion": desc},
                            screenshot_path=shot,
                            healed=result.healed,
                        ),
                        level,
                    )
            return result, level
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
            ambig = self._consume_ambiguity()
            if ambig is not None:
                return self._build_ambiguous_result(step, ambig, "click")
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
            ambig = self._consume_ambiguity()
            if ambig is not None:
                return self._build_ambiguous_result(step, ambig, "change")
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
            ambig = self._consume_ambiguity()
            if ambig is not None:
                return self._build_ambiguous_result(step, ambig, "upload")
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

    def _do_set_selection(self, step: SkillStep) -> tuple[ToolResult, int]:
        """Reconcile a multi-select picker to the operator's target list.

        Reads the current selection from current_items_selector, computes
        the diff vs the target list per mode, then opens the picker,
        searches + clicks the needed items, and closes the picker.

        Failure modes that surface as step.failed:
          - target param is not a list
          - current_items_selector resolves nothing (picker structure
            changed) AND mode=replace needs to read current state
          - a target item's checkbox can't be found at L1/L2/L3
        """
        spec = step.set_selection
        if spec is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="set_selection",
                    error="set_selection step has no spec",
                    error_kind="bad_step",
                ),
                0,
            )

        target = self.params.get(spec.param)
        if not isinstance(target, list):
            # Tolerant CSV split is convenient for CLI invocations but
            # the planner / agent layer is expected to pass a list. Log
            # the coercion so a stray-comma bug doesn't masquerade as
            # multiple items, and so an operator who passes "foo, bar"
            # as ONE item sees a warning.
            split = [t.strip() for t in str(target or "").split(",") if t.strip()]
            if len(split) > 1:
                self.audit.log(
                    "warn",
                    (
                        f"set_selection: param {spec.param!r} arrived as a "
                        f"string with commas; auto-split into {len(split)} items "
                        "-- pass a list to be explicit"
                    ),
                )
            target = split
        target_set = set(target)

        page = self.session.page

        # Read current selection.
        current_ids: list[str] = []
        if spec.current_items_selector:
            try:
                attr = spec.current_items_id_attr or "data-testid"
                prefix = spec.current_items_id_prefix or ""
                # Evaluate inside the page to read attribute values from all matches.
                current_ids = page.evaluate(
                    "([sel, attr, prefix]) => {"
                    " const out = [];"
                    " document.querySelectorAll(sel).forEach(el => {"
                    "  const v = el.getAttribute(attr) || '';"
                    "  out.push(prefix ? v.replace(prefix, '') : v);"
                    " });"
                    " return out; }",
                    [spec.current_items_selector, attr, prefix],
                )
            except Exception:
                current_ids = []

        current_set = set(current_ids)

        # Compute add / remove based on mode.
        to_add: set[str] = set()
        to_remove: set[str] = set()
        if spec.mode == "replace":
            to_add = target_set - current_set
            to_remove = current_set - target_set
        elif spec.mode == "add":
            to_add = target_set - current_set
        elif spec.mode == "remove":
            to_remove = target_set & current_set
        elif spec.mode == "preserve":
            if not (current_set & target_set):
                to_add = target_set - current_set

        # Nothing to do? success.
        if not to_add and not to_remove:
            return (
                ToolResult(
                    success=True,
                    action_taken=(
                        f"set_selection({spec.mode}, {spec.param}) no-op "
                        f"-- current already matches target"
                    ),
                ),
                1,
            )

        # Open picker if a fingerprint was recorded for it.
        if spec.open_picker_fp:
            try:
                loc = self._locate_via_template(spec.open_picker_fp, {})
                if loc:
                    loc.click(timeout=4000)
                    self._wait_for_page_settle(max_ms=2000)
            except Exception as e:
                return (
                    ToolResult(
                        success=False,
                        action_taken=f"set_selection: open_picker failed: {e}",
                        error_kind="set_selection_open_failed",
                    ),
                    0,
                )

        # For each item to add: search (if search_fp recorded) then click checkbox.
        for item in sorted(to_add):
            if spec.search_fp:
                try:
                    search_loc = self._locate_via_template(spec.search_fp, {})
                    if search_loc:
                        search_loc.fill(item)
                        # Wait for the filter to apply; the list re-renders.
                        page.wait_for_timeout(150)
                except Exception:
                    pass  # search is a convenience; the checkbox locator below is what matters
            if spec.checkbox_template_fp is None:
                return (
                    ToolResult(
                        success=False,
                        action_taken=f"set_selection: no checkbox_template_fp",
                        error_kind="bad_step",
                    ),
                    0,
                )
            try:
                loc = self._locate_via_template(
                    spec.checkbox_template_fp, {"item": item}
                )
                if loc is None:
                    return (
                        ToolResult(
                            success=False,
                            action_taken=(
                                f"set_selection: no checkbox match for "
                                f"item={item!r}"
                            ),
                            error_kind="set_selection_item_not_found",
                            error_details={"item": item},
                        ),
                        0,
                    )
                loc.click(timeout=3000)
            except Exception as e:
                return (
                    ToolResult(
                        success=False,
                        action_taken=(
                            f"set_selection: click failed for item={item!r}: {e}"
                        ),
                        error_kind="set_selection_click_failed",
                        error_details={"item": item},
                    ),
                    0,
                )

        # For each item to remove: same checkbox click (toggle semantics).
        for item in sorted(to_remove):
            if spec.checkbox_template_fp is None:
                break
            try:
                loc = self._locate_via_template(
                    spec.checkbox_template_fp, {"item": item}
                )
                if loc is not None:
                    loc.click(timeout=3000)
            except Exception:
                # Removal failures are softer -- if we can't find a chip
                # to remove it may already be gone.
                continue

        # Commit (close picker) if recorded.
        if spec.commit_fp:
            try:
                loc = self._locate_via_template(spec.commit_fp, {})
                if loc:
                    loc.click(timeout=3000)
            except Exception:
                pass

        return (
            ToolResult(
                success=True,
                action_taken=(
                    f"set_selection({spec.mode}, {spec.param}): "
                    f"+{len(to_add)} -{len(to_remove)}"
                ),
            ),
            1,
        )

    def _locate_via_template(
        self,
        fp: ElementFingerprint,
        extra_params: dict[str, str],
    ) -> Optional[Locator]:
        """Resolve a fingerprint with an extra one-shot template
        substitution (e.g. ``{"item": "sports"}``). Used by set_selection
        to materialize the per-item checkbox without mutating the
        skill's parameter context.

        Reuses the L1->L2 cascade. Doesn't trigger ambiguity detection
        because the body of a set_selection loop is by construction
        per-item; multiple matches at L1 mean the picker's testids
        aren't unique enough for the selection to be safe -- we just
        click .first in that case.
        """
        try:
            substituted = fp.model_copy(deep=True)
            if fp.templates:
                merged = dict(self.params)
                merged.update(extra_params)
                for field, tmpl in fp.templates.items():
                    try:
                        resolved = tmpl.format(**merged)
                    except KeyError:
                        continue
                    setattr(substituted, field, resolved)
            page = self.session.page
            loc = self._level1(page, substituted)
            if loc is not None:
                return loc
            return self._level2(page, substituted)
        except Exception:
            return None

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

        # Operator override path: if a previous run failed with
        # ambiguous_target and the operator picked a specific candidate,
        # the orchestrator threads that pick down to us as
        # sub_step_overrides[step.index]. Apply it BEFORE ambiguity
        # detection so the override doesn't itself trip the multi-match
        # check (the override IS the disambiguation).
        override = self._sub_step_overrides.pop(step.index, None)
        if override:
            fp = self._apply_locator_override(fp, override)
            self.audit.log(
                "info",
                f"step {step.index} using operator-picked locator override",
                data={"override": override},
            )
            diag = getattr(self, "_diag", None)
            if diag is not None:
                diag["used_operator_override"] = True

        # Ambiguity check: if the recording captured a SPECIFIC element
        # (templated fingerprint) but at replay the materialized locator
        # resolves to multiple visible elements, the recorded action was
        # targeted at one of them -- we don't know which. Surface as
        # ambiguous_target instead of silently picking .first. Skipped
        # when an override is in play -- the override is the answer to
        # the ambiguity.
        if not override:
            ambig = self._detect_ambiguity(page, fp, step)
            if ambig:
                # If we have a persisted disambiguation hint for this
                # step (operator picked one of these candidates on a
                # prior run), score the current candidates against the
                # hint. A clear winner short-circuits the pause; the
                # ambiguity gets resolved automatically. This is the
                # learning loop.
                hint = self.disambiguation_hints.get(step.index)
                resolved = (
                    self._resolve_with_hint(page, hint, ambig)
                    if hint
                    else None
                )
                if resolved is not None:
                    self.audit.log(
                        "info",
                        (
                            f"step {step.index} ambiguity auto-resolved by hint: "
                            f"{resolved.get('test_id') or resolved.get('id') or '?'}"
                        ),
                    )
                    if diag := getattr(self, "_diag", None):
                        diag["used_operator_override"] = True
                        diag["ambiguity_candidate_count"] = len(ambig)
                    fp = self._apply_locator_override(fp, resolved)
                    # Fall through to L1 below with the hint-resolved fp.
                else:
                    self._pending_ambiguity = ambig
                    if diag := getattr(self, "_diag", None):
                        diag["ambiguity_candidate_count"] = len(ambig)
                    return None, 0, None

        diag = getattr(self, "_diag", None)

        # --- Level 1 — exact stable attributes ----
        if diag is not None:
            diag["levels_attempted"].append(1)
        for candidate in self._fp_with_alternates(fp):
            l1 = self._level1(page, candidate)
            if l1 is not None:
                return l1, 1, None

        # --- Level 2 — semantic ----
        if diag is not None:
            diag["levels_attempted"].append(2)
        for candidate in self._fp_with_alternates(fp):
            l2 = self._level2(page, candidate)
            if l2 is not None:
                return l2, 2, None

        # --- Level 3 — self-heal via locator_repair ----
        if diag is not None:
            diag["levels_attempted"].append(3)
        return self._level3(page, fp, step.semantic_label)

    def _resolve_with_hint(
        self,
        page: Page,
        hint: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> Optional[dict[str, str]]:
        """Pick a candidate that matches the persisted hint, or None.

        Strict-first matching:
          1. test_id exact match
          2. text substring match (case-insensitive)
          3. negative candidates exclusion -- if the current candidates
             contain all of the previously-rejected ones plus one new
             one, the new one is the winner by elimination.

        Returns a small dict {test_id?, id?} suitable for
        ``_apply_locator_override``, or None if no candidate wins
        confidently. We err conservative: ambiguity is better than
        silently picking the wrong row.
        """
        # 1) test_id exact match.
        chosen_id = hint.get("chosen_test_id")
        if chosen_id:
            for c in candidates:
                if c.get("test_id") == chosen_id:
                    return {"test_id": chosen_id}

        # 2) text substring match.
        chosen_text = (hint.get("chosen_text") or "").strip().lower()
        if chosen_text:
            for c in candidates:
                ctext = (c.get("text") or "").strip().lower()
                if ctext and chosen_text in ctext:
                    if c.get("test_id"):
                        return {"test_id": c["test_id"]}
                    if c.get("id"):
                        return {"id": c["id"]}

        # 3) Negative-candidate elimination. If the current candidate
        # set is exactly (previously-rejected ∪ {one new}), the new
        # one is the answer. Conservative: requires the previously-
        # rejected to all be present in current candidates.
        neg = hint.get("negative_candidates") or []
        if neg:
            neg_ids = {n.get("test_id") for n in neg if n.get("test_id")}
            current_ids = {
                c.get("test_id") for c in candidates if c.get("test_id")
            }
            new_ones = current_ids - neg_ids
            if neg_ids.issubset(current_ids) and len(new_ones) == 1:
                only = next(iter(new_ones))
                return {"test_id": only}
        return None

    def _detect_ambiguity(
        self,
        page: Page,
        fp: ElementFingerprint,
        step: SkillStep,
    ) -> Optional[list[dict[str, Any]]]:
        """Return a list of candidate elements when the recording
        captured one specific element but replay finds multiple.

        Only fires when the fingerprint had templated fields *and* the
        templated value at replay would resolve to >1 visible element.
        Untemplated fingerprints are skipped -- "click first match"
        was the recording's own behavior, so reproducing it is correct.
        """
        if not fp.templates:
            return None

        candidates_per_attr: list[list[dict[str, Any]]] = []

        # Templated test_id is the most common case (e.g. row-{id}).
        if fp.test_id and "test_id" in fp.templates:
            try:
                loc = page.get_by_test_id(fp.test_id)
                count = loc.count()
                if count > 1:
                    visible = self._collect_candidate_summaries(loc, count)
                    if len(visible) > 1:
                        candidates_per_attr.append(visible)
            except Exception:
                pass

        # Templated id (less common but possible).
        if fp.element_id and "element_id" in fp.templates:
            try:
                loc = page.locator(f"#{_css_escape(fp.element_id)}")
                count = loc.count()
                if count > 1:
                    visible = self._collect_candidate_summaries(loc, count)
                    if len(visible) > 1:
                        candidates_per_attr.append(visible)
            except Exception:
                pass

        if not candidates_per_attr:
            return None
        # If multiple templated attrs are ambiguous, surface the first;
        # they're usually pointing at the same elements anyway.
        return candidates_per_attr[0]

    def _collect_candidate_summaries(
        self, locator: Locator, count: int
    ) -> list[dict[str, Any]]:
        """Build operator-readable summaries for each match. Used as the
        candidates list in ambiguous_target.error_details so the UI can
        render a row picker.
        """
        out: list[dict[str, Any]] = []
        for i in range(min(count, 8)):  # cap at 8 — UI doesn't need more
            try:
                nth = locator.nth(i)
                if not nth.is_visible(timeout=300):
                    continue
                summary = nth.evaluate(
                    "el => ({"
                    " test_id: el.getAttribute('data-testid'),"
                    " id: el.id || null,"
                    " text: (el.innerText || el.textContent || '').trim().slice(0, 120),"
                    " role: el.getAttribute('role'),"
                    " tag: el.tagName.toLowerCase()"
                    "})"
                )
                out.append({"index": i, **summary})
            except Exception:
                continue
        return out

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

    def _apply_locator_override(
        self, fp: ElementFingerprint, override: dict[str, Any]
    ) -> ElementFingerprint:
        """Return a copy of ``fp`` with identifying fields replaced by
        ``override`` so L1 resolution targets the operator's pick directly.

        Strongest-first: if the override carries a test_id, it wipes the
        original test_id AND clears element_id/name/aria_label so they
        don't keep matching the wrong element. If the override only has
        an element_id, use that. The fallback chain (L2 semantic, L3
        heal) still runs on the override, so a stale pick still has
        recovery -- it just no longer fires ambiguous_target since the
        operator already disambiguated.
        """
        out = fp.model_copy(deep=True)
        if override.get("test_id"):
            out.test_id = override["test_id"]
            out.element_id = None
            out.name = None
        elif override.get("id"):
            out.element_id = override["id"]
            out.test_id = None
        # Wipe templates so _materialize_fingerprint doesn't re-substitute
        # the param value over the override on the next loop iteration.
        out.templates = {}
        return out

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
        if diag := getattr(self, "_diag", None):
            diag["heal_backend"] = result.backend
            diag["heal_confidence"] = result.confidence
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

    # Spinner-style transient state indicators. Used as an *additional*
    # signal alongside in-flight network and DOM-quiescence -- not the
    # primary one. Portals that follow this convention get faster
    # detection of "still saving"; portals that don't lose nothing.
    _SPINNER_SELECTOR = (
        "[data-testid^='status-saving'],"
        "[data-testid^='status-applying'],"
        "[data-testid^='status-searching'],"
        "[data-testid^='loading-'],"
        "[data-testid^='spinner-'],"
        "[data-testid='loading'],"
        "[data-testid='spinner'],"
        "[role='progressbar']"
    )

    # JS injected into the page on first wait. Mirrors the watchers the
    # grabber installs during teach -- replay needs the same signals
    # whether or not the operator ran teach with our overlay attached.
    # Maintains:
    #   __cp_inflight       count of active fetch+XHR requests
    #   __cp_last_request_at ms epoch of most recent request boundary
    #   __cp_last_mutation_at ms epoch of most recent DOM mutation
    #   __cp_request_log    ring buffer of the last ~50 completed
    #                       requests: { url, method, status, ts, finished_ts }
    # The request log is what makes expected_signals.network matching
    # possible at runtime -- the runner asks the page "have you seen
    # any /api/markets request finish in the last N seconds?" without
    # needing CDP-level network interception.
    _WATCHER_INSTALL_JS = r"""
    () => {
      if (window.__cp_quiescence_installed) return true;
      window.__cp_quiescence_installed = true;
      window.__cp_last_mutation_at = Date.now();
      window.__cp_inflight = 0;
      window.__cp_last_request_at = 0;
      window.__cp_request_log = [];
      const LOG_CAP = 50;
      function _logRequest(entry) {
        const log = window.__cp_request_log;
        log.push(entry);
        if (log.length > LOG_CAP) log.shift();
      }
      const root = document.body || document.documentElement;
      if (root) {
        try {
          new MutationObserver(() => {
            window.__cp_last_mutation_at = Date.now();
          }).observe(root, {
            childList: true, subtree: true, attributes: true, characterData: true,
          });
        } catch (e) {}
      }
      if (window.fetch && !window.__cp_fetch_hooked) {
        window.__cp_fetch_hooked = true;
        const _f = window.fetch.bind(window);
        window.fetch = function (input, init) {
          const url = typeof input === 'string' ? input : (input && input.url) || '';
          const method = (init && init.method) || (input && input.method) || 'GET';
          const started_ts = Date.now();
          window.__cp_inflight = (window.__cp_inflight || 0) + 1;
          window.__cp_last_request_at = started_ts;
          let p;
          try { p = _f.apply(this, arguments); }
          catch (e) {
            window.__cp_inflight = Math.max(0, window.__cp_inflight - 1);
            _logRequest({ url, method, status: 0, started_ts, finished_ts: Date.now(), error: true });
            throw e;
          }
          return p.then(
            r => {
              window.__cp_inflight = Math.max(0, window.__cp_inflight - 1);
              window.__cp_last_request_at = Date.now();
              _logRequest({ url, method, status: r.status, started_ts, finished_ts: Date.now() });
              return r;
            },
            e => {
              window.__cp_inflight = Math.max(0, window.__cp_inflight - 1);
              window.__cp_last_request_at = Date.now();
              _logRequest({ url, method, status: 0, started_ts, finished_ts: Date.now(), error: true });
              throw e;
            }
          );
        };
      }
      if (window.XMLHttpRequest && !window.__cp_xhr_hooked) {
        window.__cp_xhr_hooked = true;
        const _open = window.XMLHttpRequest.prototype.open;
        const _send = window.XMLHttpRequest.prototype.send;
        window.XMLHttpRequest.prototype.open = function (method, url) {
          this.__cp_method = method;
          this.__cp_url = url;
          return _open.apply(this, arguments);
        };
        window.XMLHttpRequest.prototype.send = function () {
          const self = this;
          const started_ts = Date.now();
          window.__cp_inflight = (window.__cp_inflight || 0) + 1;
          window.__cp_last_request_at = started_ts;
          self.addEventListener('loadend', () => {
            window.__cp_inflight = Math.max(0, window.__cp_inflight - 1);
            window.__cp_last_request_at = Date.now();
            _logRequest({
              url: self.__cp_url || '', method: self.__cp_method || 'GET',
              status: self.status || 0, started_ts, finished_ts: Date.now(),
              error: self.status === 0,
            });
          });
          return _send.apply(self, arguments);
        };
      }
      return true;
    }
    """

    def _ensure_watchers(self, page: Page) -> None:
        """Install the in-page quiescence watchers if they aren't already.

        Idempotent -- safe to call before every wait. Failures here are
        non-fatal; we just fall back to the simpler spinner check."""
        try:
            page.evaluate(self._WATCHER_INSTALL_JS)
            page.evaluate(self._IDEMPOTENCY_INSTALL_JS)
        except Exception:
            pass

    # JS that installs a fetch+XHR shim adding an Idempotency-Key header
    # derived from window.__cp_step_idem (set by _set_step_idem_context
    # before each step). The shim only fires on state-changing methods
    # (POST/PATCH/PUT/DELETE) and never overrides an existing key that
    # the app already chose. Combined with the sample portal's
    # Idempotency-Key middleware, this means two retry attempts of the
    # same step issue the same key for the same URL and the second
    # attempt returns the cached response -- no double-publish.
    _IDEMPOTENCY_INSTALL_JS = r"""
    () => {
      if (window.__cp_idem_installed) return true;
      window.__cp_idem_installed = true;
      window.__cp_step_idem = window.__cp_step_idem || null;

      // Strip query string + hash from URL when building the idempotency
      // key -- two retries against the same logical endpoint should
      // dedupe even if one sends a cursor param the other doesn't.
      function _idemUrl(urlStr) {
        if (!urlStr) return '';
        let u = String(urlStr);
        const q = u.indexOf('?');
        if (q >= 0) u = u.slice(0, q);
        const h = u.indexOf('#');
        if (h >= 0) u = u.slice(0, h);
        return u;
      }

      function _augment(headers, method, urlStr) {
        const m = (method || 'GET').toUpperCase();
        if (m === 'GET' || m === 'HEAD' || m === 'OPTIONS') return headers;
        const ctx = window.__cp_step_idem;
        if (!ctx) return headers;
        const h = new Headers(headers || {});
        if (h.has('Idempotency-Key')) return h;
        h.set('Idempotency-Key', ctx + ':' + _idemUrl(urlStr));
        return h;
      }

      if (window.fetch && !window.__cp_idem_fetch_wrapped) {
        window.__cp_idem_fetch_wrapped = true;
        const _f = window.fetch.bind(window);
        window.fetch = function (input, init) {
          try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            const method = (init && init.method) || (input && input.method) || 'GET';
            const init2 = init ? Object.assign({}, init) : {};
            init2.headers = _augment(init.headers || {}, method, url);
            return _f.call(this, input, init2);
          } catch (e) {
            return _f.apply(this, arguments);
          }
        };
      }

      if (window.XMLHttpRequest && !window.__cp_idem_xhr_wrapped) {
        window.__cp_idem_xhr_wrapped = true;
        const _setRH = window.XMLHttpRequest.prototype.setRequestHeader;
        const _send = window.XMLHttpRequest.prototype.send;
        window.XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
          if (k && k.toLowerCase() === 'idempotency-key') {
            this.__cp_idem_already_set = true;
          }
          return _setRH.apply(this, arguments);
        };
        window.XMLHttpRequest.prototype.send = function () {
          try {
            const method = (this.__cp_method || 'GET').toUpperCase();
            const url = this.__cp_url || '';
            if (
              method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS' &&
              !this.__cp_idem_already_set && window.__cp_step_idem
            ) {
              _setRH.call(this, 'Idempotency-Key',
                          window.__cp_step_idem + ':' + _idemUrl(url));
            }
          } catch (e) {}
          return _send.apply(this, arguments);
        };
      }
      return true;
    }
    """

    def _set_step_idem_context(self, page: Page, step: SkillStep) -> None:
        """Set the per-step idempotency context on the page.

        Combines the runner's session_id with the step.index so:
          - retries of the SAME step within the SAME run share the key
            (the backend's Idempotency-Key middleware dedupes them)
          - the next replay run gets fresh keys (new session_id), so
            stale cache entries from yesterday don't leak.
        Failures here are non-fatal -- idempotency is a safety net,
        not a correctness requirement -- but we log them so a silent
        regression doesn't go undetected when a real customer cares.
        """
        try:
            ctx = f"replay:{self.session_id}:{step.index}"
            page.evaluate(
                "(ctx) => { window.__cp_step_idem = ctx; }", ctx
            )
        except Exception as e:  # noqa: BLE001
            self.audit.log(
                "warn",
                (
                    f"idempotency context not set for step {step.index} "
                    f"({type(e).__name__}: {e}) -- retries of this step "
                    "may double-write to destructive endpoints"
                ),
            )

    def _wait_for_page_settle(
        self,
        max_ms: int = 4000,
        expected: Optional["ExpectedSignals"] = None,
    ) -> None:
        """Wait *only when needed* for the page to be ready for the next action.

        When ``expected`` is provided (per-step expected_signals from
        the skill), we wait specifically for those network + DOM
        targets. When it isn't, we fall back to the generic in-flight
        + DOM-quiescence heuristic.

        Either path is bounded by ``max_ms`` and returns immediately
        when conditions are already satisfied.

        ``portal_network_ignore`` (read off self.portal_network_ignore
        if set by the caller) lets the runner skip URLs that match a
        portal-level ignore list -- noisy notifications channels, SSE,
        etc. -- so they don't keep ``inflight`` artificially high.
        """
        page = self.session.page
        self._ensure_watchers(page)
        diag = getattr(self, "_diag", None)

        # Path A: targeted waits driven by the skill's expected_signals.
        if expected is not None:
            self._wait_for_expected_signals(page, expected, diag, max_ms)
            return

        # Path B: generic heuristic for skills that don't have signals yet.
        deadline_ms = max_ms

        t0 = time.monotonic()
        try:
            page.wait_for_function(
                self._inflight_predicate_js(),
                timeout=deadline_ms,
            )
        except Exception:
            pass
        if diag is not None:
            diag["waits_ms"]["network"] = int((time.monotonic() - t0) * 1000)

        t0 = time.monotonic()
        try:
            page.wait_for_function(
                "() => { const t = window.__cp_last_mutation_at || 0;"
                "        return t > 0 && (Date.now() - t) > 250; }",
                timeout=min(deadline_ms, 2000),
            )
        except Exception:
            pass
        if diag is not None:
            diag["waits_ms"]["dom"] = int((time.monotonic() - t0) * 1000)

        t0 = time.monotonic()
        try:
            spinner = page.locator(self._SPINNER_SELECTOR)
            if spinner.count() > 0:
                try:
                    spinner.first.wait_for(state="hidden", timeout=1500)
                except Exception:
                    pass
        except Exception:
            pass
        if diag is not None:
            diag["waits_ms"]["spinner"] = int((time.monotonic() - t0) * 1000)

    def _inflight_predicate_js(self) -> str:
        """Build the JS predicate "all current network is idle".

        Honors portal_network_ignore: requests whose URLs match any
        substring in the ignore list don't count. Since __cp_inflight
        is a raw counter we can't filter inside it; instead we look at
        __cp_request_log to *also* require "no non-ignored request
        finished in the last 250ms," which catches both the in-flight
        and the very-recently-finished cases.
        """
        ignore_patterns = list(getattr(self, "portal_network_ignore", []) or [])
        ignore_js = json.dumps(ignore_patterns)
        quiet_ms = int(getattr(self, "network_quiet_ms", 250) or 250)
        return (
            f"() => {{ const ignore = {ignore_js};"
            f"  const QUIET_MS = {quiet_ms};"
            "  const inflight = window.__cp_inflight || 0;"
            "  if (ignore.length === 0) return inflight === 0;"
            "  const log = window.__cp_request_log || [];"
            "  const now = Date.now();"
            "  const recentNonIgnored = log.some(r =>"
            "    (now - r.finished_ts) < QUIET_MS &&"
            "    !ignore.some(p => (r.url || '').toLowerCase().includes(p.toLowerCase()))"
            "  );"
            "  if (recentNonIgnored) return false;"
            "  return inflight === 0; }"
        )

    def _wait_for_expected_signals(
        self,
        page: Page,
        expected: "ExpectedSignals",
        diag: Optional[dict[str, Any]],
        max_ms: int,
    ) -> None:
        """Wait for each declared network + DOM target. Required
        targets fail the step; optional targets log but don't fail.
        """
        # Reset the per-step __cp_opt_seen tracker so a "stable count"
        # observed during step N doesn't auto-pass during step N+1.
        # Each step that declares options_changed/count_changed starts
        # with a fresh "no count seen yet" state.
        try:
            page.evaluate("() => { window.__cp_opt_seen = {}; }")
        except Exception:
            pass

        # Network: poll the page's request log for matches.
        t0 = time.monotonic()
        for ne in expected.network:
            pattern = ne.url_pattern.lower()
            timeout_ms = min(ne.max_ms, max_ms)
            try:
                page.wait_for_function(
                    "([pat, method, statusFilter]) => {"
                    " const log = window.__cp_request_log || [];"
                    " return log.some(r =>"
                    "   (r.url || '').toLowerCase().includes(pat) &&"
                    "   (!method || (r.method || 'GET').toUpperCase() === method.toUpperCase()) &&"
                    "   (statusFilter === null || r.status === statusFilter) &&"
                    "   r.finished_ts > 0"
                    " ); }",
                    arg=[pattern, ne.method, ne.status],
                    timeout=timeout_ms,
                )
                self.audit.log(
                    "info",
                    f"expected_signal matched: {ne.method} {ne.url_pattern}",
                )
            except Exception:
                if not ne.optional:
                    self.audit.log(
                        "warn",
                        (
                            f"expected_signal MISSING (required): "
                            f"{ne.method} {ne.url_pattern} -- "
                            f"step may have raced"
                        ),
                    )
        if diag is not None:
            diag["waits_ms"]["network"] = int((time.monotonic() - t0) * 1000)

        # DOM: poll for the declared condition.
        t0 = time.monotonic()
        for de in expected.dom:
            try:
                if de.kind in ("visible", "hidden"):
                    state = "visible" if de.kind == "visible" else "hidden"
                    page.locator(de.selector).first.wait_for(
                        state=state, timeout=de.timeout_ms
                    )
                elif de.kind == "options_changed":
                    # Wait until the option count is different from
                    # whatever we observed first time we looked.
                    page.wait_for_function(
                        "([sel, stable]) => {"
                        " const els = document.querySelectorAll(sel);"
                        " if (els.length === 0) return false;"
                        " const now = Date.now();"
                        " window.__cp_opt_seen = window.__cp_opt_seen || {};"
                        " const last = window.__cp_opt_seen[sel];"
                        " if (last == null) {"
                        "   window.__cp_opt_seen[sel] = { count: els.length, ts: now };"
                        "   return false;"
                        " }"
                        " if (els.length !== last.count) {"
                        "   window.__cp_opt_seen[sel] = { count: els.length, ts: now };"
                        "   return false;"
                        " }"
                        " return (now - last.ts) >= stable; }",
                        arg=[de.selector, de.stable_ms],
                        timeout=de.timeout_ms,
                    )
                elif de.kind == "count_changed":
                    # Same pattern -- count-based detection.
                    page.wait_for_function(
                        "([sel, stable]) => {"
                        " const els = document.querySelectorAll(sel);"
                        " const now = Date.now();"
                        " window.__cp_opt_seen = window.__cp_opt_seen || {};"
                        " const last = window.__cp_opt_seen[sel];"
                        " if (last == null) {"
                        "   window.__cp_opt_seen[sel] = { count: els.length, ts: now };"
                        "   return false;"
                        " }"
                        " if (els.length !== last.count) {"
                        "   window.__cp_opt_seen[sel] = { count: els.length, ts: now };"
                        "   return false;"
                        " }"
                        " return (now - last.ts) >= stable; }",
                        arg=[de.selector, de.stable_ms],
                        timeout=de.timeout_ms,
                    )
            except Exception:
                self.audit.log(
                    "warn",
                    f"expected dom signal missing: kind={de.kind} sel={de.selector}",
                )
        if diag is not None:
            diag["waits_ms"]["dom"] = int((time.monotonic() - t0) * 1000)

    def _verify_assertions(self, step: SkillStep) -> tuple[bool, Optional[str]]:
        """Run step.assert_after assertions in order.

        Returns (ok, failing_description). ok=True if all pass or no
        assertions declared. Failure returns ok=False and a short
        operator-readable description of which assertion missed.
        """
        if not step.assert_after:
            return True, None
        page = self.session.page
        for a in step.assert_after:
            try:
                ok = self._check_one_assertion(page, a)
            except Exception as e:
                return False, f"{a.kind}: exception {e}"
            if not ok:
                return False, self._describe_assertion(a)
        return True, None

    def _check_one_assertion(
        self, page: Page, a: "StepAssertion"
    ) -> bool:
        if a.kind == "visible" and a.selector:
            try:
                page.locator(a.selector).first.wait_for(
                    state="visible", timeout=a.timeout_ms
                )
                return True
            except Exception:
                return False
        if a.kind == "hidden" and a.selector:
            try:
                page.locator(a.selector).first.wait_for(
                    state="hidden", timeout=a.timeout_ms
                )
                return True
            except Exception:
                return False
        if a.kind in ("count_eq", "count_gte", "count_lte") and a.selector:
            try:
                count = page.locator(a.selector).count()
            except Exception:
                return False
            expected = a.n or 0
            if a.kind == "count_eq":
                return count == expected
            if a.kind == "count_gte":
                return count >= expected
            return count <= expected
        if a.kind == "text_contains" and a.text:
            try:
                if a.selector:
                    el_text = page.locator(a.selector).first.inner_text(
                        timeout=a.timeout_ms
                    )
                else:
                    el_text = page.locator("body").inner_text(
                        timeout=a.timeout_ms
                    )
                return a.text.lower() in (el_text or "").lower()
            except Exception:
                return False
        if a.kind == "url_contains" and a.text:
            return a.text.lower() in (page.url or "").lower()
        if a.kind == "attr_equals" and a.selector and a.attr:
            try:
                val = page.locator(a.selector).first.get_attribute(
                    a.attr, timeout=a.timeout_ms
                )
                return val == a.text
            except Exception:
                return False
        return False

    def _describe_assertion(self, a: "StepAssertion") -> str:
        parts = [a.kind]
        if a.selector:
            parts.append(f"selector={a.selector!r}")
        if a.text:
            parts.append(f"text={a.text!r}")
        if a.n is not None:
            parts.append(f"n={a.n}")
        return " ".join(parts)

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
        if diag := getattr(self, "_diag", None):
            diag["post_condition_passed"] = passed
        return passed

    # ---- Human takeover ----------------------------------------------------

    def _consume_ambiguity(self) -> Optional[list[dict[str, Any]]]:
        ambig = self._pending_ambiguity
        self._pending_ambiguity = None
        return ambig

    def _build_ambiguous_result(
        self,
        step: SkillStep,
        candidates: list[dict[str, Any]],
        verb: str,
    ) -> tuple[ToolResult, int]:
        """Emit a step.failed-shaped ToolResult with structured candidates
        for the orchestrator's pause flow + UI row picker."""
        shot = self._screenshot(f"step_{step.index}_ambiguous")
        self.audit.log(
            "info",
            f"step {step.index} ambiguous_target: {len(candidates)} candidates",
            data={
                "label": step.semantic_label,
                "candidates": candidates,
            },
        )
        return (
            ToolResult(
                success=False,
                action_taken=(
                    f"step {step.index} {verb} target ambiguous "
                    f"({len(candidates)} matches)"
                ),
                error=(
                    f"ambiguous_target: {len(candidates)} elements match the "
                    "templated locator -- the recording targeted one specific element"
                ),
                error_kind="ambiguous_target",
                error_details={"candidates": candidates, "verb": verb},
                screenshot_path=shot,
            ),
            0,
        )

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
    base_url: str = "http://localhost:5188",
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
