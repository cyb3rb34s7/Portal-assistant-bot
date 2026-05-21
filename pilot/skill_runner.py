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
from typing import Any, Callable, Literal, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeoutError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from .audit import AuditLogger
from .browser import BrowserSession, connect_to_chrome
from .models import Diagnostic, LocatorProbeResult, ToolResult
from .param_codecs import ParamValidationError, resolve_param
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


# WI-01: action types declared in the schema but whose runner
# implementation lands in subsequent WIs. Dispatching to a stub gives
# operators a clear "this WI hasn't shipped yet" error instead of a
# silent no-op when an annotator (current or future) emits one of these.
# As each WI lands, the action type moves from this set to its real
# handler in _execute_step.
_UNIMPLEMENTED_ACTIONS: frozenset[str] = frozenset({
    # Implemented in subsequent WIs and removed from this set:
    #   fill_submit (WI-15), select_autocomplete (WI-16),
    #   select_option (WI-17), date_select (WI-21).
    "slider_set",           # WI-28
    "drag_drop",             # WI-30
    "toggle_state",         # WI-33
    "modal",                # WI-34
    "popup",                # WI-35
    "download",             # WI-45
    "scroll_until",         # WI-38
    "rich_text_set",        # WI-39
    "shortcut",             # WI-41
    "canvas_gesture",       # WI-49
})


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
        # WI-04: portal idempotency capability. None disables injection
        # entirely. The RealExecutor sets this from PortalContext.idempotency
        # at construction; legacy callers leave it None.
        self.idempotency_capability: Optional[Any] = None
        # Disambiguation hints keyed by step.index. Carries operator
        # picks from prior runs so we can resolve ambiguity without
        # pausing again. Set externally by the executor reading the
        # skill's .hints.json sidecar.
        self.disambiguation_hints: dict[int, dict[str, Any]] = {}
        # F-09d: baseline timestamps for expected_signals.started_after_event.
        # Populated at the start of each step with that step's
        # started-at ms; expected_signals on later steps that reference
        # an earlier step's event_id (the step.provenance.raw_event_ids
        # entry) resolve to that recorded ts. None means "no scoping"
        # and the baseline check is a no-op (preserves legacy behavior
        # for skills that don't declare started_after_event).
        self._event_baseline_ts: dict[str, int] = {}
        # Set by _resolve_locator when L1 finds >1 match for a templated
        # locator. Action methods read it to emit ambiguous_target
        # instead of falling through to generic L4 takeover. Cleared on
        # every step entry.
        self._pending_ambiguity: list[dict[str, Any]] | None = None

        # WI-06: structured-diagnostic accumulator. Each call to
        # ``_diagnostic`` appends a Diagnostic to this list AND logs via
        # the existing audit pipeline so the JSONL record is searchable
        # later. The orchestrator (or a host inspecting a runner-only
        # session) can read self.diagnostics to surface them through
        # the replay event stream.
        self.diagnostics: list[Diagnostic] = []

        # WI-09: per-portal wait policy. Set by the executor from
        # PortalContext.wait_policy at construction; defaults to the
        # WaitPolicy schema's class-level defaults when None. The
        # runner reads request_log_cap from here when installing the
        # page-side watcher and uses the implicit step-start baseline
        # (self._step_started_ms) for action-scoped network waits.
        self.wait_policy: Optional[Any] = None
        self._step_started_ms: int = 0
        """WI-09: timestamp (ms epoch) of the current step's start.
        Set in _execute_step at the moment _set_step_idem_context runs.
        Network expected_signals that don't carry an explicit
        started_after_event baseline scope to this timestamp -- a
        request that FINISHED before the step started cannot satisfy
        the wait. Closes the BLOCKER-4 stale-request hole."""

    # ---- WI-06: diagnostics --------------------------------------------

    def _diagnostic(
        self,
        code: str,
        *,
        level: str = "warn",
        recoverable: bool = True,
        step_index: Optional[int] = None,
        **context,
    ) -> None:
        """Record a structured diagnostic for a previously-silent
        failure site. Each call:
          1. Appends a Diagnostic to self.diagnostics (in-memory list
             so the host can surface them after run() returns).
          2. Writes to the audit log so the JSONL artifact carries the
             same record (same shape as other audit entries plus a
             ``diagnostic_code`` field).

        Choose ``code`` from the WI-06 stable set: ``runner.*`` for
        skill_runner failures, ``executor.*`` for executor_real
        failures, ``teach.*`` for teach failures. Tests assert on
        these codes."""
        if step_index is None:
            diag = getattr(self, "_diag", None)
            if isinstance(diag, dict):
                step_index = diag.get("step_index")
        d = Diagnostic(
            code=code,
            context=context,
            recoverable=recoverable,
            level=level,  # type: ignore[arg-type]
        )
        self.diagnostics.append(d)
        try:
            self.audit.log(
                level if level in ("warn", "error", "debug") else "warn",
                f"diagnostic[{code}]: {context}",
                data={
                    "diagnostic_code": code,
                    "diagnostic_context": context,
                    "recoverable": recoverable,
                    "step_index": step_index,
                },
            )
        except Exception:
            # Never let the audit emit itself crash the runner.
            pass

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
            # F-08e: drain any idempotency-shim diagnostics that accrued
            # during this step. The page-side shim's catch blocks push
            # entries into a window queue; we surface them through the
            # audit log here so failures aren't invisible.
            try:
                self._drain_idempotency_diagnostics(self.session.page, step)
            except Exception:
                pass
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
                # WI-07: respect the step's replay_policy.on_failure
                # instead of always continuing. Default policy on
                # SkillStep is on_failure="abort", which means the
                # runner stops the skill here -- orchestrator pause /
                # retry / skip is the recovery surface.
                #
                # Policies:
                #   "abort"    -- stop the skill (default for new schema)
                #   "continue" -- log + proceed; for non-critical steps
                #   "optional" -- like continue but error doesn't propagate;
                #                 the step's failure isn't a skill failure
                #   "recover"  -- reserved for future WI-26 (runner-level
                #                 recovery hooks); behaves as abort today
                #
                # The ``optional`` flag is a convenience equivalent to
                # on_failure="optional".
                policy = step.replay_policy
                effective = (
                    "optional" if policy.optional else policy.on_failure
                )
                if effective in ("abort", "recover"):
                    self.audit.log(
                        "info",
                        (
                            f"replay aborted at step {step.index} "
                            f"(policy={effective}); orchestrator pause "
                            "flow takes over"
                        ),
                        data={"step_index": step.index},
                    )
                    break
                # continue / optional: fall through

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

        # F-09d + WI-09: record this step's start time against each of
        # its raw_event_ids so expected_signals.started_after_event on
        # a LATER step can scope its match to "started after this
        # step." int(time.time()*1000) keeps the units identical to
        # the page-side __cp_request_log started_ts values.
        # WI-09 also stores the timestamp on self._step_started_ms as
        # the IMPLICIT baseline -- network waits whose
        # NetworkExpectation.started_after_event is None still scope
        # to the action's start, so a request that FINISHED before
        # the step kicked off cannot accidentally satisfy the wait.
        step_start_ms = int(time.time() * 1000)
        self._step_started_ms = step_start_ms
        if step.provenance and step.provenance.raw_event_ids:
            for raw_id in step.provenance.raw_event_ids:
                if raw_id:
                    self._event_baseline_ts[raw_id] = step_start_ms

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

        try:
            value = self._resolved_value(step)
        except ParamValidationError as pve:
            # WI-05: typed codec / constraint failure. Surface as a
            # structured error BEFORE the page is touched. The
            # orchestrator's pause flow shows the operator the
            # diagnostic and lets them retry/skip/abort.
            shot = self._screenshot(f"step_{step.index}_param_validation")
            return (
                ToolResult(
                    success=False,
                    action_taken=f"param validation: {pve.message}",
                    error=str(pve),
                    error_kind="param_validation_failed",
                    error_details={
                        "param": pve.param_name,
                        "step_index": step.index,
                        **pve.details,
                    },
                    screenshot_path=shot,
                ),
                0,
            )

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
            elif step.action == "fill_submit":
                result, level = self._do_fill_submit(step, value)
            elif step.action == "select_autocomplete":
                result, level = self._do_select_autocomplete(step)
            elif step.action == "select_option":
                result, level = self._do_select_option(step, value)
            elif step.action == "date_select":
                result, level = self._do_date_select(step)
            elif step.action in _UNIMPLEMENTED_ACTIONS:
                result, level = self._do_unimplemented_action(step)
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
        # WI-08: standalone navigate steps are reserved for explicit
        # operator intent (manual address-bar nav + initial_load). For
        # those: one navigation attempt. Don't fall back to a SECOND
        # goto with domcontentloaded on timeout -- that was the audit's
        # "page.goto(...networkidle...), then second goto" bug: the
        # second goto reloaded the SPA and re-fired initial fetches,
        # which could race with the previous step's mutation.
        #
        # The right behavior on timeout is to wait for the current load
        # state (whatever's available) and continue, rather than
        # repeating the navigation. networkidle's 500ms-quiet window is
        # usually enough for SPAs, and a long-polling portal that never
        # reaches networkidle still needs us NOT to bounce twice.
        try:
            self.session.page.goto(url, wait_until="networkidle", timeout=15000)
        except PWTimeoutError:
            try:
                self.session.page.wait_for_load_state(
                    "domcontentloaded", timeout=5000
                )
            except Exception:
                # Even DOM content load timed out -- surface but don't
                # re-goto. The next step's locator wait will fail
                # explicitly if the page truly isn't ready.
                self._diagnostic(
                    "runner.navigate_load_state_timeout",
                    level="warn",
                    recoverable=True,
                    url=url,
                )
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
        # WI-08: capture URL before the click so we can verify the
        # navigation effect (if declared) without relying on the page
        # already being at the right place.
        url_before = ""
        try:
            url_before = page.url or ""
        except Exception:
            pass
        # WI-14: pick the click API based on the annotator's gesture
        # classification. ``double`` => Playwright dblclick(); ``toggle``
        # / ``open`` / ``close`` use a single click but the assertion
        # path will verify the target state (future WI-33 handler may
        # short-circuit when desired state is already met). ``single``
        # / ``repeat`` / unknown fall back to plain .click(). We do NOT
        # multiply clicks for ``repeat`` here -- repeat is preserved
        # as a SEMANTIC marker; the annotator emits one step per
        # recorded click. The runner could batch repeats if a future
        # detector (WI-33 toggle, WI-37 paginated tables) declared a
        # count, but the conservative default is one click per step.
        gesture = step.click_gesture or "single"
        if gesture == "double":
            click_fn = lambda: locator.dblclick(timeout=4000)  # noqa: E731
        else:
            click_fn = lambda: locator.click(timeout=4000)  # noqa: E731
        verified = self._execute_with_heal_check(
            page, level, heal, click_fn
        )
        # WI-08: when the recording captured a navigation effect for
        # this click, wait for the URL to settle to the templated value
        # and assert the route. NEVER call page.goto() -- the click is
        # what produces the navigation, and forcing a goto would
        # accidentally reload the SPA at the wrong asset (the audit's
        # BLOCKER-1 wrong-asset bug).
        nav_result_ok = True
        nav_error: Optional[str] = None
        if (
            verified
            and step.effects is not None
            and step.effects.navigation is not None
        ):
            nav_result_ok, nav_error = self._assert_nav_effect(
                page, step, url_before
            )
            if not nav_result_ok:
                self._diagnostic(
                    "runner.click_nav_effect_unmet",
                    level="warn",
                    recoverable=True,
                    error=nav_error,
                    url_before=url_before,
                )
        shot = self._screenshot(f"step_{step.index}_click")
        result, ret_level = self._build_action_result(
            success=verified and nav_result_ok,
            level=level,
            heal=heal,
            action_taken=f"clicked {step.semantic_label} [{LEVEL_LABELS[level]}]",
            screenshot_path=shot,
            unverified_error="L3 heal: page state did not change after click",
        )
        if verified and not nav_result_ok and result.success is False:
            # Tag with a specific error_kind so the operator sees
            # "navigation effect missed" instead of generic step
            # failure.
            result = ToolResult(
                success=False,
                action_taken=result.action_taken,
                error=nav_error or "navigation effect not satisfied",
                error_kind="navigation_effect_unmet",
                error_details={"url_before": url_before},
                screenshot_path=result.screenshot_path,
                healed=result.healed,
            )
        return result, ret_level

    def _assert_nav_effect(
        self,
        page: Page,
        step: SkillStep,
        url_before: str,
    ) -> tuple[bool, Optional[str]]:
        """WI-08: verify a click's declared navigation effect.

        Sequence:
          1. Wait briefly for the URL to differ from url_before (the
             click produced a route change). 3s is enough for SPA
             routers; the WaitPolicy will widen this in a follow-up WI.
          2. If a url_template / assert_url is declared, render it
             through self.params and assert a substring match (or
             fall back to checking the literal url).
          3. Honor effects.navigation.timeout_ms when present.

        Returns (ok, error_message). ``error_message`` is None on
        success and a structured short string on failure.
        """
        eff = step.effects.navigation  # type: ignore[union-attr]
        assert eff is not None
        timeout_ms = eff.timeout_ms or 5000

        # 1) Wait for URL to change away from url_before. If the
        # template / literal url already matched url_before (e.g. a
        # popstate to a different URL that round-trips), we proceed.
        # Prefer the template when available -- it survives different
        # param values at replay. A literal URL (no template) is the
        # legacy migration case: we treat it as a "URL changed" hint
        # rather than a hard equality assert, because the literal
        # recorded URL is by definition wrong at replay (the whole
        # point of WI-08 is that the click produces the right URL,
        # not the recorded one).
        target_template = eff.assert_url or eff.url_template or ""
        use_template_match = bool(eff.url_template) or (
            target_template and "{" in target_template
        )
        # Render template with current params for the assertion. If
        # rendering fails (missing param), fall back to the literal.
        rendered_target = target_template
        try:
            if "{" in target_template and "}" in target_template:
                rendered_target = target_template.format(**self.params)
        except (KeyError, IndexError):
            rendered_target = target_template
        # If no template was usable, treat as URL-changed assertion
        # only (don't try to match the literal URL).
        if not use_template_match:
            rendered_target = ""

        def _url_satisfies(url: str) -> bool:
            if not url:
                return False
            if rendered_target:
                # Strip protocol+host so comparisons are route-only:
                # the recording's url is typically http://localhost:port/
                # while replay may be on a different host.
                def _route(u: str) -> str:
                    if "://" in u:
                        u = u.split("://", 1)[1]
                    if "/" in u:
                        u = u[u.find("/"):]
                    return u
                if _route(rendered_target) in _route(url):
                    return True
                return False
            # No template -- just require the URL changed.
            return url != url_before

        try:
            page.wait_for_function(
                "([before, target]) => {"
                " const u = location.href;"
                " if (!u) return false;"
                " function route(s) {"
                "  if (s.indexOf('://') >= 0) s = s.split('://')[1];"
                "  const slash = s.indexOf('/');"
                "  return slash >= 0 ? s.slice(slash) : s;"
                " }"
                " if (target) return route(u).includes(route(target));"
                " return u !== before; }",
                arg=[url_before, rendered_target],
                timeout=timeout_ms,
            )
        except Exception:
            current = ""
            try:
                current = page.url or ""
            except Exception:
                pass
            return False, (
                f"URL did not match expected route {rendered_target!r} "
                f"within {timeout_ms}ms (current={current!r})"
            )
        return True, None

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
        # WI-13: when the annotator marked this step as clear_intent,
        # the operator deliberately erased the field. Use an empty
        # string as the resolved value regardless of what the param
        # resolution returned -- this is a structural intent, not a
        # value substitution. Without this, a runtime param defaulting
        # to None / "" would still call fill() with whatever the codec
        # produced, and a missing-param error would mask the clear.
        is_clear_intent = bool(
            step.value_transition and step.value_transition.clear_intent
        )
        if is_clear_intent:
            value = ""
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
        # WI-13: verify the field is actually empty after a clear. The
        # Playwright fill('') call clears the input value, but a
        # framework like React may re-populate from state on the next
        # render. Reading input_value() after the fill confirms the
        # clear stuck; we fail with a specific error_kind so the
        # operator sees "clear didn't stick" instead of generic
        # post_condition_failed.
        clear_verified = True
        clear_error: Optional[str] = None
        if (
            verified
            and is_clear_intent
            and tag != "select"
            and input_type not in ("checkbox", "radio")
        ):
            try:
                actual = locator.input_value(timeout=2000)
                if actual != "":
                    clear_verified = False
                    clear_error = (
                        f"clear_intent step: field still holds {actual!r}"
                    )
            except Exception as e:
                clear_verified = False
                clear_error = f"clear_intent verify: {type(e).__name__}: {e}"
        shot = self._screenshot(f"step_{step.index}_change")
        result, ret_level = self._build_action_result(
            success=verified and clear_verified,
            level=level,
            heal=heal,
            action_taken=(
                f"cleared {step.semantic_label} [{LEVEL_LABELS[level]}]"
                if is_clear_intent
                else f"set {step.semantic_label} = {value!r} [{LEVEL_LABELS[level]}]"
            ),
            screenshot_path=shot,
            unverified_error="L3 heal: page state did not change after fill",
        )
        if verified and not clear_verified:
            result = ToolResult(
                success=False,
                action_taken=result.action_taken,
                error=clear_error or "clear did not stick",
                error_kind="clear_intent_unmet",
                error_details={"step_index": step.index},
                screenshot_path=result.screenshot_path,
                healed=result.healed,
            )
        return result, ret_level

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

    def _do_unimplemented_action(
        self, step: SkillStep
    ) -> tuple[ToolResult, int]:
        """Stub for ActionTypes introduced in WI-01 (schema-only) but
        whose runner behavior lands in later WIs.

        Fails loudly so an operator sees exactly which WI is pending
        and so a skill that uses a new action type can't silently
        succeed without actually doing the action. The orchestrator's
        pause flow takes over from here per the step's replay_policy.
        """
        self.audit.log(
            "warn",
            (
                f"step {step.index} action {step.action!r} declared in "
                "schema but runner implementation not yet shipped"
            ),
            data={"action": step.action},
        )
        return (
            ToolResult(
                success=False,
                action_taken=f"step {step.index} {step.action} (stub)",
                error=(
                    f"action {step.action!r} is declared in the schema but "
                    "its runner implementation has not landed yet. See "
                    "DOCS/reviews/2026-05-21_fix-everything-plan.md for "
                    "the work item that will implement it."
                ),
                error_kind="action_not_implemented",
                error_details={"action": step.action},
            ),
            0,
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

        # WI-20: if this set_selection depends on a parent picker, wait
        # for the option-source request to finish refreshing the child
        # picker's options BEFORE reading current state. The parent
        # step is responsible for changing the parent picker value;
        # WI-20's contract is "child options must be refreshed before
        # this step reconciles."
        if spec.option_source is not None:
            from .skill_models import ExpectedSignals as _ES
            self._wait_for_page_settle(
                expected=_ES(network=[spec.option_source])
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
            except Exception as e:
                # WI-06: was silent. Failed current-selection read used
                # to make set_selection believe the picker was empty,
                # so it added everything (mode=replace) regardless of
                # what was actually selected. Surface so the operator
                # sees the structural read failed.
                self._diagnostic(
                    "runner.set_selection_current_read_failed",
                    level="warn",
                    recoverable=True,
                    exc_type=type(e).__name__,
                    exc_msg=str(e)[:200],
                    selector=spec.current_items_selector,
                    attr=attr,
                    prefix=prefix,
                )
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
                except Exception as e:
                    # WI-06: search is a convenience; we still proceed
                    # to the checkbox locator. But surface the failure
                    # so the operator can see when the picker's search
                    # field stopped matching.
                    self._diagnostic(
                        "runner.set_selection_search_failed",
                        level="warn",
                        recoverable=True,
                        exc_type=type(e).__name__,
                        exc_msg=str(e)[:200],
                        item=item,
                    )
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
            except Exception as e:
                # WI-06: Removal failures are softer -- if we can't
                # find a chip to remove it may already be gone. But
                # surface the diagnostic so the operator notices when
                # this is repeatedly failing instead of believing the
                # set-selection succeeded.
                self._diagnostic(
                    "runner.set_selection_remove_failed",
                    level="warn",
                    recoverable=True,
                    exc_type=type(e).__name__,
                    exc_msg=str(e)[:200],
                    item=item,
                )
                continue

        # Commit (close picker) if recorded.
        if spec.commit_fp:
            try:
                loc = self._locate_via_template(spec.commit_fp, {})
                if loc:
                    loc.click(timeout=3000)
            except Exception as e:
                # WI-06: commit-click failure used to disappear silently
                # -- a picker that didn't close left the next step's
                # locator inside the popover. Surface so the operator
                # sees the structural commit failed.
                self._diagnostic(
                    "runner.set_selection_commit_failed",
                    level="warn",
                    recoverable=True,
                    exc_type=type(e).__name__,
                    exc_msg=str(e)[:200],
                )

        # WI-19: final equality assertion. After reconciliation, read
        # the chip set again and verify it equals the target set
        # EXACTLY. Catches the silent set_selection_remove_failed +
        # set_selection_commit_failed bugs WI-06 surfaced as warnings
        # but did NOT fail-stop on. Default True per WI-19; operator
        # can opt out by declaring final_equality_assertion=False.
        if spec.final_equality_assertion and spec.current_items_selector:
            try:
                attr = spec.current_items_id_attr or "data-testid"
                prefix = spec.current_items_id_prefix or ""
                final_ids = page.evaluate(
                    "([sel, attr, prefix]) => {"
                    " const out = [];"
                    " document.querySelectorAll(sel).forEach(el => {"
                    "  const v = el.getAttribute(attr) || '';"
                    "  out.push(prefix ? v.replace(prefix, '') : v);"
                    " });"
                    " return out; }",
                    [spec.current_items_selector, attr, prefix],
                )
                final_set = set(final_ids or [])
                # mode-specific expected final state:
                if spec.mode == "replace":
                    expected = target_set
                elif spec.mode == "add":
                    expected = current_set | target_set
                elif spec.mode == "remove":
                    expected = current_set - target_set
                elif spec.mode == "preserve":
                    expected = (
                        current_set
                        if (current_set & target_set)
                        else (current_set | target_set)
                    )
                else:
                    expected = target_set
                if final_set != expected:
                    return (
                        ToolResult(
                            success=False,
                            action_taken=(
                                f"set_selection({spec.mode}, {spec.param}) "
                                f"final equality assertion failed"
                            ),
                            error=(
                                f"set_selection_equality_failed: expected "
                                f"{sorted(expected)!r}, got "
                                f"{sorted(final_set)!r}"
                            ),
                            error_kind="set_selection_equality_failed",
                            error_details={
                                "expected": sorted(expected),
                                "actual": sorted(final_set),
                                "to_add": sorted(to_add),
                                "to_remove": sorted(to_remove),
                            },
                        ),
                        0,
                    )
            except Exception as e:
                self._diagnostic(
                    "runner.set_selection_equality_read_failed",
                    level="warn",
                    recoverable=True,
                    exc_type=type(e).__name__,
                    exc_msg=str(e)[:200],
                )

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

    def _do_fill_submit(
        self, step: SkillStep, value: Optional[str]
    ) -> tuple[ToolResult, int]:
        """WI-15: fill a text input ONCE with the final value, then
        trigger the declared submit mechanism exactly ONCE.

        Replaces the legacy multi-step replay (fill, fill, fill, key
        Enter, click submit) that re-fired requests and races against
        autocomplete results from intermediate values. The recording's
        FinalValueSpec carries:
          - submit_trigger: "enter" | "button" | "form_submit"
          - value_param: name of the resolved value param
          - submit_button_fp: optional, for "button" trigger
          - expected_result_signal: optional URL substring to wait on
        """
        spec = step.fill_submit
        if spec is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="fill_submit",
                    error="fill_submit step has no spec",
                    error_kind="bad_step",
                ),
                0,
            )

        # Resolve the target value. _resolved_value already ran in
        # _execute_step; ``value`` is the codec'd output. Fall back to
        # step.value for legacy traces.
        final_value = value if value is not None else (step.value or "")

        locator, level, heal = self._resolve_locator(step)
        if locator is None:
            ambig = self._consume_ambiguity()
            if ambig is not None:
                return self._build_ambiguous_result(step, ambig, "fill_submit")
            return self._fallback_human(
                step, "could not locate fill_submit field"
            )

        page = self.session.page

        # Step 1: fill the resolved value ONCE.
        try:
            locator.fill(final_value, timeout=5000)
        except Exception as e:
            shot = self._screenshot(f"step_{step.index}_fill_submit_fill")
            return (
                ToolResult(
                    success=False,
                    action_taken=f"fill_submit: fill failed: {e}",
                    error=str(e),
                    error_kind="fill_submit_fill_failed",
                    screenshot_path=shot,
                    healed=heal,
                ),
                level,
            )

        # Step 2: trigger the declared submit mechanism ONCE.
        try:
            if spec.submit_trigger == "enter":
                locator.press("Enter", timeout=4000)
            elif spec.submit_trigger == "form_submit":
                # Playwright's form.submit() isn't directly accessible
                # via Locator. Press Enter on the field -- the form's
                # onsubmit handler still fires. For inputs not inside
                # a form, this is a no-op (which is consistent with
                # how a recorded form_submit event without a wrapping
                # form would behave at replay).
                locator.press("Enter", timeout=4000)
            elif spec.submit_trigger == "button":
                if spec.submit_button_fp is None:
                    return (
                        ToolResult(
                            success=False,
                            action_taken="fill_submit: button trigger",
                            error="submit_trigger=button but no submit_button_fp",
                            error_kind="bad_step",
                        ),
                        0,
                    )
                btn = self._locate_via_template(spec.submit_button_fp, {})
                if btn is None:
                    return (
                        ToolResult(
                            success=False,
                            action_taken="fill_submit: button trigger",
                            error="submit button not found",
                            error_kind="fill_submit_button_not_found",
                        ),
                        0,
                    )
                btn.click(timeout=4000)
        except Exception as e:
            shot = self._screenshot(f"step_{step.index}_fill_submit_trigger")
            return (
                ToolResult(
                    success=False,
                    action_taken=f"fill_submit: trigger failed: {e}",
                    error=str(e),
                    error_kind="fill_submit_trigger_failed",
                    screenshot_path=shot,
                    healed=heal,
                ),
                level,
            )

        # Step 3: wait for the declared result signal if present.
        # When expected_result_signal is set we scope through
        # _wait_for_page_settle's network expectation; otherwise rely
        # on the post-step generic settle that the executor runs.
        if spec.expected_result_signal:
            from .skill_models import (
                ExpectedSignals as _ES, NetworkExpectation as _NE,
            )
            self._wait_for_page_settle(
                expected=_ES(
                    network=[_NE(
                        url_pattern=spec.expected_result_signal,
                        method="GET",  # search endpoints are typically GET
                        optional=False,
                    )]
                )
            )

        shot = self._screenshot(f"step_{step.index}_fill_submit")
        return self._build_action_result(
            success=True,
            level=level,
            heal=heal,
            action_taken=(
                f"fill_submit ({spec.submit_trigger}, "
                f"{step.semantic_label}={final_value!r}) "
                f"[{LEVEL_LABELS[level]}]"
            ),
            screenshot_path=shot,
            unverified_error="fill_submit: post action verify failed",
        )

    def _do_select_autocomplete(
        self, step: SkillStep
    ) -> tuple[ToolResult, int]:
        """WI-16: fill query, wait for declared search response, locate
        the result row by identity (provenance-templated fingerprint),
        click. Fail with option_not_in_results on absence.

        The recording's AutocompleteSpec carries:
          - query_param: name of the typed-text param,
          - selected_item_param: name of the identity param for the
            row to click (distinct from query_param so the operator
            can pick A-9002 even though the recording picked A-9003),
          - query_input_fp: the search input fingerprint,
          - result_container_fp: optional container to wait for,
          - option_identity_template: fingerprint with a placeholder
            that materializes to the row to click,
          - network_expectation: the search request URL to wait on
            before clicking the result.
        """
        spec = step.select_autocomplete
        if spec is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="select_autocomplete",
                    error="select_autocomplete step has no spec",
                    error_kind="bad_step",
                ),
                0,
            )

        query_value = self.params.get(spec.query_param)
        if query_value is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="select_autocomplete",
                    error=f"missing query param {spec.query_param!r}",
                    error_kind="param_missing",
                ),
                0,
            )
        selected_value = self.params.get(spec.selected_item_param)
        if selected_value is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="select_autocomplete",
                    error=(
                        f"missing selected_item param "
                        f"{spec.selected_item_param!r}"
                    ),
                    error_kind="param_missing",
                ),
                0,
            )

        page = self.session.page

        # Step 1: locate query input and fill it.
        # Prefer the spec's query_input_fp; fall back to step.fingerprint.
        query_fp = spec.query_input_fp or step.fingerprint
        if query_fp is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="select_autocomplete",
                    error="no query input fingerprint",
                    error_kind="bad_step",
                ),
                0,
            )
        query_loc = self._locate_via_template(query_fp, {})
        if query_loc is None:
            return self._fallback_human(
                step, "could not locate autocomplete query input"
            )
        try:
            query_loc.fill(str(query_value), timeout=5000)
        except Exception as e:
            return (
                ToolResult(
                    success=False,
                    action_taken=f"autocomplete: fill failed: {e}",
                    error=str(e),
                    error_kind="autocomplete_fill_failed",
                ),
                0,
            )

        # Step 2: wait for the search response.
        if spec.network_expectation is not None:
            from .skill_models import ExpectedSignals as _ES
            self._wait_for_page_settle(
                expected=_ES(network=[spec.network_expectation])
            )
        else:
            # Generic settle when no expectation declared.
            self._wait_for_page_settle()

        # Step 3: locate the result row by selected_item and click.
        if spec.option_identity_template is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="autocomplete: missing option_identity_template",
                    error_kind="bad_step",
                ),
                0,
            )
        # The template fingerprint may carry templates referencing the
        # selected_item_param; merge it in. We use _locate_via_template
        # which already merges self.params -- self.params already
        # contains spec.selected_item_param, so this Just Works.
        result_loc = self._locate_via_template(
            spec.option_identity_template, {}
        )
        if result_loc is None:
            return (
                ToolResult(
                    success=False,
                    action_taken=(
                        f"autocomplete: result {selected_value!r} "
                        f"not found"
                    ),
                    error=(
                        f"option_not_in_results: query={query_value!r} "
                        f"selected_item={selected_value!r}"
                    ),
                    error_kind="option_not_in_results",
                    error_details={
                        "query": str(query_value),
                        "selected_item": str(selected_value),
                    },
                ),
                0,
            )
        try:
            result_loc.click(timeout=4000)
        except Exception as e:
            shot = self._screenshot(f"step_{step.index}_autocomplete_click")
            return (
                ToolResult(
                    success=False,
                    action_taken=f"autocomplete: result click failed: {e}",
                    error=str(e),
                    error_kind="autocomplete_click_failed",
                    screenshot_path=shot,
                ),
                0,
            )

        shot = self._screenshot(f"step_{step.index}_autocomplete")
        return (
            ToolResult(
                success=True,
                action_taken=(
                    f"select_autocomplete(query={query_value!r}, "
                    f"selected={selected_value!r})"
                ),
                screenshot_path=shot,
            ),
            1,
        )

    def _do_select_option(
        self, step: SkillStep, value: Optional[str]
    ) -> tuple[ToolResult, int]:
        """WI-17: native single-select with declared-or-fail semantics.

        Match priority:
          1. exact ``value`` match against rendered option.value,
          2. exact ``label`` match against rendered option.text,
          3. declared aliases (operator-set) -- look up the resolved
             value as a key in spec.aliases and try each alternative
             label/value,
          4. current_options mode: pick by recorded_index from the
             currently-rendered options,
          5. fail with error_kind=option_not_available listing what
             IS available.

        Replaces the legacy ``_select_option_with_fuzzy_fallback`` for
        select_option-clustered steps. The legacy code path is kept
        intact for back-compat with non-clustered ``change`` events on
        <select> elements (legacy skills).
        """
        spec = step.select_option
        if spec is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="select_option",
                    error="select_option step has no spec",
                    error_kind="bad_step",
                ),
                0,
            )

        locator, level, heal = self._resolve_locator(step)
        if locator is None:
            ambig = self._consume_ambiguity()
            if ambig is not None:
                return self._build_ambiguous_result(step, ambig, "select_option")
            return self._fallback_human(step, "could not locate select target")

        # Operator-resolved value via the param binding (or step.value).
        resolved = value if value is not None else (spec.recorded_value or "")

        # Read the current options to validate against. The recording's
        # options_snapshot is the at-record-time view; at replay the
        # rendered set may differ for state-dependent dropdowns. We
        # ALWAYS read current options so failure messages list what's
        # actually available now.
        try:
            current_options = locator.evaluate(
                "el => Array.from(el.options).map(o => "
                "({ value: o.value, text: (o.textContent || '').trim() }))"
            )
        except Exception as e:
            return (
                ToolResult(
                    success=False,
                    action_taken="select_option",
                    error=(
                        f"could not read current options: "
                        f"{type(e).__name__}: {e}"
                    ),
                    error_kind="select_option_read_failed",
                ),
                0,
            )
        if not current_options:
            return (
                ToolResult(
                    success=False,
                    action_taken="select_option",
                    error="select has no options at replay",
                    error_kind="option_not_available",
                    error_details={"available": []},
                ),
                0,
            )

        current_values = {o.get("value", "") for o in current_options}
        current_labels = {
            (o.get("text") or "").strip() for o in current_options
        }
        target_value: Optional[str] = None

        # current_options mode: pick by index.
        if spec.match_mode == "current_options":
            idx = spec.recorded_index
            if idx is None or idx < 0 or idx >= len(current_options):
                return (
                    ToolResult(
                        success=False,
                        action_taken="select_option",
                        error=(
                            f"current_options mode requires recorded_index "
                            f"in range; got {idx}, options={len(current_options)}"
                        ),
                        error_kind="option_not_available",
                        error_details={
                            "available": [o.get("value") for o in current_options],
                        },
                    ),
                    0,
                )
            target_value = current_options[idx].get("value")
        else:
            # 1. exact value
            if resolved in current_values:
                target_value = resolved
            # 2. exact label
            elif spec.match_mode in ("label", "alias") or resolved in current_labels:
                for o in current_options:
                    if (o.get("text") or "").strip() == resolved:
                        target_value = o.get("value")
                        break
            # 3. declared aliases
            if (
                target_value is None
                and spec.match_mode == "alias"
                and spec.aliases
            ):
                alts = spec.aliases.get(resolved, [])
                for alt in alts:
                    if alt in current_values:
                        target_value = alt
                        break
                    for o in current_options:
                        if (o.get("text") or "").strip() == alt:
                            target_value = o.get("value")
                            break
                    if target_value is not None:
                        break

        if target_value is None:
            available_labels = ", ".join(
                f"{o.get('value')!r}({(o.get('text') or '').strip()!r})"
                for o in current_options[:12]
            )
            return (
                ToolResult(
                    success=False,
                    action_taken="select_option",
                    error=(
                        f"option_not_available: requested {resolved!r} not in "
                        f"current options. Available: [{available_labels}]"
                    ),
                    error_kind="option_not_available",
                    error_details={
                        "requested": resolved,
                        "match_mode": spec.match_mode,
                        "available": [
                            {"value": o.get("value"), "text": o.get("text")}
                            for o in current_options
                        ],
                    },
                ),
                0,
            )

        # Apply the selection.
        try:
            locator.select_option(value=target_value, timeout=3000)
        except Exception as e:
            return (
                ToolResult(
                    success=False,
                    action_taken=f"select_option apply failed: {e}",
                    error=str(e),
                    error_kind="select_option_apply_failed",
                    healed=heal,
                ),
                level,
            )

        # WI-18: when this select is the PARENT of a cascading
        # dependency, wait for the child's option-refresh request
        # before letting subsequent steps proceed. The child step's
        # _do_select_option will then read the refreshed options.
        if step.dependency_chain is not None:
            dep = step.dependency_chain
            if dep.option_source_request is not None:
                from .skill_models import ExpectedSignals as _ES
                self._wait_for_page_settle(
                    expected=_ES(network=[dep.option_source_request])
                )

        shot = self._screenshot(f"step_{step.index}_select_option")
        return self._build_action_result(
            success=True,
            level=level,
            heal=heal,
            action_taken=(
                f"select_option({step.semantic_label}={target_value!r}) "
                f"[{LEVEL_LABELS[level]}]"
            ),
            screenshot_path=shot,
            unverified_error="select_option: post-action verify failed",
        )

    def _do_date_select(self, step: SkillStep) -> tuple[ToolResult, int]:
        """WI-21: native or custom date picker.

        Native: locate the date input and set its value to the
        resolved ISO date (the WI-05 ``iso_date`` codec normalizes
        any operator input). Dispatch input + change events so React/
        Vue controlled inputs pick up the new value.

        Custom: navigate the calendar widget by SEMANTIC target date.
        Read the month_year_label_fp to determine the currently
        displayed month/year, advance/retreat via next/prev_month_fp
        until the target month is showing, then click the day cell
        matching the target day. DOES NOT replay the recorded click
        sequence (different target date may need different
        navigation).
        """
        spec = step.date_select
        if spec is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="date_select",
                    error="date_select step has no spec",
                    error_kind="bad_step",
                ),
                0,
            )

        target_date = self.params.get(spec.value_param)
        if target_date is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="date_select",
                    error=f"missing date param {spec.value_param!r}",
                    error_kind="param_missing",
                ),
                0,
            )
        # Codec resolution (iso_date) ran via _resolved_value already
        # for the binding path; for date_select we pull the param
        # directly. Ensure ISO format (codec'd by _resolved_value
        # when there's a binding; otherwise canonicalize here).
        date_str = str(target_date)

        if spec.kind == "native":
            locator, level, heal = self._resolve_locator(step)
            if locator is None:
                return self._fallback_human(
                    step, "could not locate native date input"
                )
            try:
                # locator.fill works for type=date inputs; Playwright
                # then dispatches the input + change events.
                locator.fill(date_str, timeout=4000)
            except Exception as e:
                return (
                    ToolResult(
                        success=False,
                        action_taken=f"date_select native fill failed: {e}",
                        error=str(e),
                        error_kind="date_select_fill_failed",
                    ),
                    0,
                )
            shot = self._screenshot(f"step_{step.index}_date_select")
            return self._build_action_result(
                success=True,
                level=level,
                heal=heal,
                action_taken=f"date_select(native {spec.value_param}={date_str!r})",
                screenshot_path=shot,
                unverified_error="date_select: post-action verify failed",
            )

        # Custom: navigate calendar widget by semantic date. Today's
        # implementation is conservative: open the picker via the
        # step.fingerprint (treated as the trigger), then attempt to
        # click the day cell matching the target day via the
        # day_cell_template_fp. Full month/year navigation is a
        # WI-21 follow-up when calendar_controls fingerprints land
        # in real recordings; for now we surface an actionable
        # diagnostic if the target month isn't already showing.
        page = self.session.page
        if spec.day_cell_template_fp is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="date_select",
                    error=(
                        "custom date_select: no day_cell_template_fp; "
                        "calendar navigation not yet supported for this "
                        "widget shape"
                    ),
                    error_kind="date_select_custom_unsupported",
                ),
                0,
            )

        # Parse the target day from the ISO date (YYYY-MM-DD).
        try:
            target_day = int(date_str.split("-")[2])
        except Exception:
            return (
                ToolResult(
                    success=False,
                    action_taken="date_select",
                    error=(
                        f"could not parse target date {date_str!r} for "
                        "custom calendar widget"
                    ),
                    error_kind="date_select_parse_failed",
                ),
                0,
            )

        # Substitute {day} into the template fingerprint and click.
        cell = self._locate_via_template(
            spec.day_cell_template_fp, {"day": str(target_day)}
        )
        if cell is None:
            return (
                ToolResult(
                    success=False,
                    action_taken="date_select",
                    error=(
                        f"day cell for day={target_day} not found in current "
                        "calendar view. Month navigation not yet implemented."
                    ),
                    error_kind="date_select_day_not_found",
                    error_details={"target_day": target_day, "target_date": date_str},
                ),
                0,
            )
        try:
            cell.click(timeout=3000)
        except Exception as e:
            return (
                ToolResult(
                    success=False,
                    action_taken=f"date_select cell click failed: {e}",
                    error=str(e),
                    error_kind="date_select_click_failed",
                ),
                0,
            )
        shot = self._screenshot(f"step_{step.index}_date_select_custom")
        return (
            ToolResult(
                success=True,
                action_taken=f"date_select(custom {spec.value_param}={date_str!r})",
                screenshot_path=shot,
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

        # WI-05: typed codec + constraints. The runner looks up the
        # declared SkillParam (the binding only carries name + mode) and
        # passes the operator-provided value through its codec, then
        # validates constraints. A failure raises ParamValidationError
        # which the caller converts to ToolResult.error_kind=
        # "param_validation_failed" so the page is never touched.
        sk_param = self._lookup_skill_param(binding.name)
        if sk_param is not None and (sk_param.codec != "raw"
                                     or sk_param.constraints is not None):
            try:
                resolved = resolve_param(sk_param, provided)
            except ParamValidationError:
                # Bubble up; ``_execute_step`` catches and converts.
                raise
            # The runner's other action handlers expect a single string;
            # string_list params are consumed via params dict directly
            # by _do_set_selection (which reads self.params[spec.param]).
            if isinstance(resolved, list):
                # Stash back so set_selection sees a real list, not the
                # raw operator input (e.g. comma string).
                self.params[binding.name] = resolved
                return None
            return resolved
        return str(provided)

    def _lookup_skill_param(self, name: str):
        """WI-05: find the SkillParam declaration for a binding name.

        ``Skill.params`` is a small list (skills with hundreds of params
        do not exist in practice), so linear scan is fine. Returns None
        when the binding name is not declared as a skill param --
        legacy skills with auto-named bindings fall through to
        pass-through str() coercion.
        """
        for p in self.skill.params:
            if p.name == name:
                return p
        return None

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

    # WI-22: actions that mutate portal state. These default to
    # ambiguity_policy="fail_if_multiple" when the step's
    # AmbiguityPolicy is unset. Non-mutating actions (wait, assert,
    # key without enter, navigate) default to "prompt" so the
    # operator gets a row picker but the workflow can continue when
    # they confirm.
    _DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
        "click", "change", "fill_submit", "select_option",
        "select_autocomplete", "set_selection", "upload",
        "date_select", "modal", "drag_drop", "download",
        "rich_text_set", "shortcut",
    })

    def _effective_ambiguity_policy(self, step: SkillStep) -> tuple[
        int, Literal["fail_if_multiple", "pick_first", "prompt"], list[str],
    ]:
        """WI-22: resolve the step's ambiguity policy with defaults.

        Returns (expected_count, policy, context_fields). The default
        policy depends on the action: destructive actions
        ``fail_if_multiple``, others ``prompt``. Skills authored before
        WI-22 carry no ``ambiguity_policy``; they get the safer-by-
        default behavior automatically.
        """
        pol = step.ambiguity_policy
        if pol is not None:
            return (
                pol.expected_candidate_count,
                pol.ambiguity_policy,
                pol.candidate_context_fields,
            )
        # Default: destructive actions fail-closed; everything else
        # prompts so the operator can confirm.
        is_destructive = (
            step.action in self._DESTRUCTIVE_ACTIONS or bool(step.requires_gate)
        )
        return (
            1,
            "fail_if_multiple" if is_destructive else "prompt",
            ["test_id", "text", "id", "role"],
        )

    def _detect_ambiguity(
        self,
        page: Page,
        fp: ElementFingerprint,
        step: SkillStep,
    ) -> Optional[list[dict[str, Any]]]:
        """WI-22: detect multiple visible candidates across L1 / L2 /
        alternates and emit ``ambiguous_target`` BEFORE clicking ``.first``.

        Pre-WI-22 this only fired for templated test_id / element_id.
        That missed the common L2 case: many rows share an accessible
        name like 'Open' or 'Approve', and even the templated test_id
        narrowed the recording's pick but the runner fell through to L2
        when the test_id drifted. Now: count candidates from EVERY
        attempted locator until something is uniquely visible OR
        ambiguity surfaces.

        Returns the candidate summary list when ambiguity was found,
        respecting ``ambiguity_policy``. None when no ambiguity OR when
        policy is ``pick_first``.
        """
        expected_count, policy, fields = self._effective_ambiguity_policy(step)
        if expected_count <= 0:
            # Opt-out (expected_candidate_count=0): caller wants the
            # legacy "click first match" behavior. Skip detection.
            return None
        if policy == "pick_first":
            return None

        # Track every locator we considered. If none clears the
        # "uniquely visible" bar, we surface the broadest candidate list
        # so the operator picker has context. Order matches resolver:
        # test_id, element_id, name, aria_label (L1) then role+name and
        # text (L2). For each, build a locator the same way the
        # resolver would, then count.
        attempts: list[tuple[str, Locator]] = []

        def _try(label: str, build):
            try:
                loc = build()
                if loc is not None:
                    attempts.append((label, loc))
            except Exception as e:
                self._diagnostic(
                    "runner.ambiguity_scan_failed",
                    level="warn",
                    recoverable=True,
                    exc_type=type(e).__name__,
                    exc_msg=str(e)[:200],
                    step_index=step.index,
                    attr=label,
                )

        # WI-22: detection now spans both locator levels, not only
        # templated test_id / element_id. Each lambda is bound to the
        # specific fingerprint field so the loop can call it lazily.
        # WI-24: use semantic locator APIs / _attr_locator for safe
        # escaping (the legacy ``#id`` + ``[name=...]`` shapes broke
        # on ``]`` / quotes / spaces / colons / non-ASCII).
        if fp.test_id:
            _try("test_id", lambda: page.get_by_test_id(fp.test_id))
        if fp.element_id:
            _try(
                "element_id",
                lambda: page.locator(self._attr_locator("id", fp.element_id)),
            )
        if fp.name:
            _try(
                "name",
                lambda: page.locator(self._attr_locator("name", fp.name)),
            )
        if fp.aria_label:
            _try(
                "aria_label",
                lambda: page.get_by_label(fp.aria_label, exact=False),
            )
        # L2 semantic attempts that often resolve to many rows on
        # enterprise portals -- this is the WI-22 audit's central case.
        if fp.role and fp.accessible_name:
            _try(
                "role_name",
                lambda: page.get_by_role(
                    fp.role, name=fp.accessible_name, exact=False
                ),
            )
        if fp.accessible_name and not fp.role:
            _try(
                "text_accessible",
                lambda: page.get_by_text(fp.accessible_name, exact=False),
            )

        # Pick the FIRST attempt that's neither empty (caller will fall
        # through) nor uniquely visible -- that's the ambiguity case
        # the operator needs to resolve. If every attempt is unique or
        # empty, no ambiguity to surface.
        for label, loc in attempts:
            try:
                cnt = loc.count()
            except Exception:
                continue
            if cnt <= expected_count:
                # Either zero (caller falls through L1 -> L2 -> L3) or
                # within the operator's declared expectation. Skip.
                continue
            visible = self._collect_candidate_summaries(loc, cnt)
            if len(visible) <= expected_count:
                # Multiple in DOM but only one visible -- not ambiguous
                # at replay time.
                continue
            # Enrich each candidate with the policy's context_fields.
            return self._enrich_candidates(visible, fields)
        return None

    def _attr_locator(self, attr: str, value: str) -> str:
        """WI-24: build a safe ``[attr="value"]`` CSS selector.

        Replaces the pre-WI-24 ``[attr='value']`` interpolation +
        ``_css_escape`` helper which only handled backslash and single
        quote. Values containing ``]``, double quotes, spaces, colons,
        non-ASCII, etc. broke the selector. The fix:
          - emit value inside double quotes
          - backslash-escape backslashes and double quotes
        Playwright accepts this form for ``page.locator``; for ID
        lookups we prefer ``get_by_test_id`` / ``[id="..."]`` over
        ``#id`` because the latter would still need ``CSS.escape``.
        """
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'[{attr}="{escaped}"]'

    def _enrich_candidates(
        self, candidates: list[dict[str, Any]], fields: list[str]
    ) -> list[dict[str, Any]]:
        """WI-22: ensure each candidate carries the policy's declared
        context fields. The _collect_candidate_summaries default already
        provides test_id / id / text / role / tag; extra fields the
        annotator declares are looked up via a follow-up evaluate in a
        future iteration. For now, ensure declared fields appear as
        None when absent so the UI can render uniform rows."""
        out: list[dict[str, Any]] = []
        for c in candidates:
            row = dict(c)
            for f in fields:
                row.setdefault(f, None)
            out.append(row)
        return out

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

    def _probe_and_branch(
        self,
        attr: str,
        loc: Locator,
    ) -> Optional[Locator]:
        """WI-23: shared probe + diagnostic emission. Returns the
        locator (``.first``) on ``visible_unique`` / ``multiple_matches``
        and None on every failure state. Hidden / detached / strict-mode
        / timeout each get a structured diagnostic so the operator sees
        WHY a locator level skipped instead of "L1 just didn't match."

        ``multiple_matches`` is permitted to return so the WI-22
        ambiguity scanner can compare candidates. The action handler
        consults ``_pending_ambiguity`` after a None result; if a level
        returns a locator but ambiguity also fires, the runner's
        existing _resolve_locator path takes precedence (it surfaces
        ambiguous_target instead of clicking)."""
        probe = _probe_locator(loc)
        if probe.state in ("visible_unique", "multiple_matches"):
            return loc.first
        if probe.state == "zero_matches":
            return None
        # WI-23: every non-success non-zero state emits a diagnostic.
        # Was previously a silent ``return False`` from _first_visible
        # that converted into "L1 just didn't match" -- masking hidden
        # elements, strict-mode errors, detached handles, and timeouts.
        self._diagnostic(
            "runner.locator_probe_failed",
            level="warn",
            recoverable=True,
            attr=attr,
            state=probe.state,
            count=probe.count,
            last_error=probe.last_error,
        )
        return None

    def _level1(self, page: Page, fp: ElementFingerprint) -> Optional[Locator]:
        if fp.test_id:
            # WI-24: get_by_test_id handles escaping internally; no
            # CSS construction needed.
            loc = page.get_by_test_id(fp.test_id)
            result = self._probe_and_branch("test_id", loc)
            if result is not None:
                return result
        if fp.element_id:
            # WI-24: use the [id="..."] attribute form instead of #id
            # so IDs containing ``]`` / quotes / spaces / colons /
            # non-ASCII resolve correctly. _css_escape only escaped
            # backslash and single quote -- the audit's gap.
            loc = page.locator(self._attr_locator("id", fp.element_id))
            result = self._probe_and_branch("element_id", loc)
            if result is not None:
                return result
        if fp.name:
            # WI-24: same fix -- the legacy `[name='{fp.name}']` was
            # never escaped at all.
            loc = page.locator(self._attr_locator("name", fp.name))
            result = self._probe_and_branch("name", loc)
            if result is not None:
                return result
        if fp.aria_label:
            loc = page.get_by_label(fp.aria_label, exact=False)
            result = self._probe_and_branch("aria_label", loc)
            if result is not None:
                return result
        return None

    def _level2(self, page: Page, fp: ElementFingerprint) -> Optional[Locator]:
        if fp.role and fp.accessible_name:
            try:
                loc = page.get_by_role(
                    fp.role, name=fp.accessible_name, exact=False
                )
                result = self._probe_and_branch("role_name", loc)
                if result is not None:
                    return result
            except Exception as e:
                self._diagnostic(
                    "runner.locator_probe_failed",
                    level="warn",
                    recoverable=True,
                    attr="role_name",
                    state="build_error",
                    last_error=str(e)[:200],
                )
        if fp.placeholder:
            loc = page.get_by_placeholder(fp.placeholder, exact=False)
            result = self._probe_and_branch("placeholder", loc)
            if result is not None:
                return result
        if fp.accessible_name:
            try:
                loc = page.get_by_text(fp.accessible_name, exact=False)
                result = self._probe_and_branch("accessible_name_text", loc)
                if result is not None:
                    return result
            except Exception as e:
                self._diagnostic(
                    "runner.locator_probe_failed",
                    level="warn",
                    recoverable=True,
                    attr="accessible_name_text",
                    state="build_error",
                    last_error=str(e)[:200],
                )
        if fp.text and len(fp.text) >= 3:
            text = fp.text.strip()[:50]
            try:
                loc = page.get_by_text(text, exact=False)
                result = self._probe_and_branch("text", loc)
                if result is not None:
                    return result
            except Exception as e:
                self._diagnostic(
                    "runner.locator_probe_failed",
                    level="warn",
                    recoverable=True,
                    attr="text",
                    state="build_error",
                    last_error=str(e)[:200],
                )
        if fp.css_path:
            try:
                loc = page.locator(fp.css_path)
                result = self._probe_and_branch("css_path", loc)
                if result is not None:
                    return result
            except Exception as e:
                self._diagnostic(
                    "runner.locator_probe_failed",
                    level="warn",
                    recoverable=True,
                    attr="css_path",
                    state="build_error",
                    last_error=str(e)[:200],
                )
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

    # WI-10: LAST-RESORT spinner selector list.
    #
    # Used ONLY when a step has no declared expected_signals at all
    # (legacy v1 skills + skills recorded before the WI-10 readiness
    # watcher landed). Modern skills carry annotator-emitted
    # DomExpectation kinds (aria_busy, disabled_until_enabled,
    # field_enabled, role_progressbar_hidden, text_transition,
    # selector_hidden) so the runner waits for the structural signal
    # the recording observed -- not a convention-based testid prefix
    # the portal may or may not use.
    #
    # The convention list still helps portals that follow it (the
    # status-saving / loading- prefix is widespread in enterprise
    # apps) but it is no longer the primary readiness mechanism.
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
      // WI-09: configurable cap. ``window.__cp_request_log_cap`` is set
      // by _set_step_idem_context (or _ensure_watchers fallback) from
      // PortalContext.wait_policy.request_log_cap. The hardcoded 50
      // that the audit flagged is now only a last-resort fallback when
      // no per-portal cap is wired.
      const LOG_CAP = (typeof window.__cp_request_log_cap === 'number'
        && window.__cp_request_log_cap > 0)
        ? window.__cp_request_log_cap : 200;
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
        non-fatal; we just fall back to the simpler spinner check.

        WI-04: the idempotency shim is installed only when the portal
        declares the capability. A portal that doesn't support
        idempotency keys (most don't have the middleware) gets no
        injection -- the previous global-injection behavior assumed
        backend semantics."""
        # WI-09: push the request_log_cap to the page BEFORE installing
        # the watcher so the LOG_CAP read in _WATCHER_INSTALL_JS sees
        # the per-portal value. Default 200 (vs the legacy hardcoded
        # 50) is sufficient for most enterprise dashboards; the
        # PortalContext.wait_policy.request_log_cap field lets the
        # operator tune it.
        cap = 200
        if self.wait_policy is not None:
            cap = int(getattr(self.wait_policy, "request_log_cap", 200) or 200)
        try:
            page.evaluate(
                "(c) => { window.__cp_request_log_cap = c; }", cap
            )
        except Exception:
            # The cap defaults to 200 inside the watcher if we couldn't
            # push it, so this isn't fatal. Diagnostic surfaces if
            # something deeper is wrong with the page.
            pass
        try:
            page.evaluate(self._WATCHER_INSTALL_JS)
            if self._idempotency_enabled():
                page.evaluate(self._IDEMPOTENCY_INSTALL_JS)
        except Exception as e:
            # WI-06: was a silent ``pass``. Watcher-install failure
            # silently leaves ``__cp_inflight`` undefined, which makes
            # the in-flight wait predicate return true unconditionally
            # (page LOOKS idle). Surface so the operator sees the
            # missing instrumentation; the runner continues with the
            # cheaper spinner-only wait.
            self._diagnostic(
                "runner.watcher_install_failed",
                level="warn",
                recoverable=True,
                exc_type=type(e).__name__,
                exc_msg=str(e)[:200],
                idempotency=self._idempotency_enabled(),
            )

    def _idempotency_enabled(self) -> bool:
        cfg = getattr(self, "idempotency_capability", None)
        if cfg is None:
            return False
        return bool(getattr(cfg, "enabled", False))

    # WI-04: capability-driven idempotency shim.
    #
    # Installed only when PortalContext.idempotency.enabled is True
    # (gated by _ensure_watchers above). The shim reads
    # window.__cp_idem_config -- set by _set_step_idem_context per
    # step -- which carries the capability data + per-step key
    # context. If config is null or disabled, the shim is a passthrough.
    #
    # The key is built from the configured ``key_components`` so the
    # portal operator controls the dedupe semantics (session + step is
    # safe for our typical workflows; adding body_hash differentiates
    # legitimate distinct PATCHes against the same URL).
    _IDEMPOTENCY_INSTALL_JS = r"""
    () => {
      if (window.__cp_idem_installed) return true;
      window.__cp_idem_installed = true;
      window.__cp_idem_config = window.__cp_idem_config || null;

      function _idemPath(urlStr) {
        if (!urlStr) return '';
        let u = String(urlStr);
        const q = u.indexOf('?');
        if (q >= 0) u = u.slice(0, q);
        const h = u.indexOf('#');
        if (h >= 0) u = u.slice(0, h);
        return u;
      }

      function _canonicalQuery(urlStr) {
        try {
          const u = new URL(urlStr, location.href);
          const entries = [];
          u.searchParams.forEach((v, k) => entries.push([k, v]));
          entries.sort((a, b) => (a[0] < b[0] ? -1 : 1));
          return entries.map(([k, v]) => k + '=' + v).join('&');
        } catch (e) { return ''; }
      }

      function _hashString(s) {
        // FNV-1a 32-bit. Sufficient for dedupe within a session;
        // not cryptographic. Stable across browser sessions.
        let h = 2166136261;
        for (let i = 0; i < s.length; i++) {
          h ^= s.charCodeAt(i);
          h = (h * 16777619) >>> 0;
        }
        return h.toString(16);
      }

      function _bodyHash(body, fields) {
        if (!body) return '';
        let payload = '';
        if (typeof body === 'string') {
          payload = body;
        } else {
          try { payload = JSON.stringify(body); } catch (e) { payload = String(body); }
        }
        if (!fields || fields.length === 0) return _hashString(payload);
        // Field-subset hashing: parse the JSON, pick declared fields,
        // hash that. If the body isn't JSON, fall back to whole-body
        // hash (operator opted into body_hash but didn't fence it).
        let obj;
        try { obj = JSON.parse(payload); } catch (e) { return _hashString(payload); }
        const subset = {};
        for (const f of fields) {
          let v = obj;
          for (const part of f.split('.')) {
            if (v && typeof v === 'object' && part in v) v = v[part];
            else { v = undefined; break; }
          }
          if (v !== undefined) subset[f] = v;
        }
        return _hashString(JSON.stringify(subset));
      }

      function _buildKey(cfg, method, urlStr, body) {
        const parts = [];
        for (const c of (cfg.key_components || [])) {
          switch (c) {
            case 'session':    parts.push(cfg.session_id || ''); break;
            case 'step_index': parts.push(String(cfg.step_index ?? '')); break;
            case 'method':     parts.push((method || 'GET').toUpperCase()); break;
            case 'url_path':   parts.push(_idemPath(urlStr)); break;
            case 'query_canonical': parts.push(_canonicalQuery(urlStr)); break;
            case 'body_hash':  parts.push(_bodyHash(body, cfg.body_hash_fields || [])); break;
          }
        }
        return parts.join('|');
      }

      function _endpointMatches(cfg, urlStr) {
        const patterns = cfg.endpoint_patterns || [];
        // F-08d: empty patterns means "match every endpoint" ONLY
        // when scope_all_endpoints is explicitly set. The Pydantic
        // validator on IdempotencyCapability already rejects the
        // ambiguous case at config time, but the page-side shim
        // re-checks because the config arrives over the wire as a
        // plain dict.
        if (patterns.length === 0) return !!cfg.scope_all_endpoints;
        const lower = String(urlStr || '').toLowerCase();
        return patterns.some(p => lower.indexOf(p.toLowerCase()) >= 0);
      }

      function _methodMatches(cfg, method) {
        const allowed = (cfg.method_patterns || [
          'POST', 'PUT', 'PATCH', 'DELETE'
        ]).map(m => m.toUpperCase());
        return allowed.indexOf((method || 'GET').toUpperCase()) >= 0;
      }

      function _augment(headers, method, urlStr, body) {
        const cfg = window.__cp_idem_config;
        if (!cfg || !cfg.enabled) return headers;
        if (!_methodMatches(cfg, method)) return headers;
        if (!_endpointMatches(cfg, urlStr)) return headers;
        const headerName = cfg.header_name || 'Idempotency-Key';
        const h = new Headers(headers || {});
        if (h.has(headerName) && cfg.allow_existing_header !== false) return h;
        h.set(headerName, _buildKey(cfg, method, urlStr, body));
        return h;
      }

      // F-08e: structured diagnostics queue for idempotency-shim
      // failures. Drained by the runner via page.evaluate() after each
      // step so swallowed exceptions become visible in the audit log
      // instead of silently letting un-augmented requests reach
      // destructive endpoints.
      if (!window.__cp_idem_diagnostics) window.__cp_idem_diagnostics = [];

      function _shouldHaveAugmented(cfg, method, urlStr) {
        // Returns true when, by config, this request was the kind we
        // were supposed to inject onto. Used by the catch blocks
        // below to decide between fail-open (a request we wouldn't
        // have touched anyway) and fail-closed (a matched destructive
        // request the shim failed to augment).
        if (!cfg || !cfg.enabled) return false;
        try {
          return _methodMatches(cfg, method) && _endpointMatches(cfg, urlStr);
        } catch (e) {
          return false;
        }
      }

      if (window.fetch && !window.__cp_idem_fetch_wrapped) {
        window.__cp_idem_fetch_wrapped = true;
        const _f = window.fetch.bind(window);
        window.fetch = function (input, init) {
          try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            const method = (init && init.method) || (input && input.method) || 'GET';
            const body = init && init.body;
            const init2 = init ? Object.assign({}, init) : {};
            const inputHeaders = (
              typeof Request !== 'undefined' && input instanceof Request
            ) ? input.headers : {};
            const seedHeaders = (init && init.headers) || inputHeaders || {};
            init2.headers = _augment(seedHeaders, method, url, body);
            return _f.call(this, input, init2);
          } catch (e) {
            // F-08e: log structured diagnostic AND, if this was a
            // matched destructive request, fail closed by rejecting
            // the call so the page sees a real error rather than the
            // request silently going through without the key.
            const cfg = window.__cp_idem_config;
            let url = '';
            let method = 'GET';
            try {
              url = typeof input === 'string' ? input : (input && input.url) || '';
              method = (init && init.method) || (input && input.method) || 'GET';
            } catch (_) {}
            window.__cp_idem_diagnostics.push({
              ts: Date.now(),
              where: 'fetch_wrap',
              error: String(e && e.message || e),
              method: method,
              url: url,
              matched: _shouldHaveAugmented(cfg, method, url),
            });
            if (_shouldHaveAugmented(cfg, method, url)) {
              return Promise.reject(new Error(
                '[cp-idempotency] shim failed on matched destructive ' +
                method + ' ' + url + ': ' + (e && e.message || e)
              ));
            }
            return _f.apply(this, arguments);
          }
        };
      }

      if (window.XMLHttpRequest && !window.__cp_idem_xhr_wrapped) {
        window.__cp_idem_xhr_wrapped = true;
        const _setRH = window.XMLHttpRequest.prototype.setRequestHeader;
        const _send = window.XMLHttpRequest.prototype.send;
        window.XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
          const cfg = window.__cp_idem_config;
          const headerName = (cfg && cfg.header_name) || 'Idempotency-Key';
          if (k && String(k).toLowerCase() === String(headerName).toLowerCase()) {
            this.__cp_idem_already_set = true;
          }
          return _setRH.apply(this, arguments);
        };
        window.XMLHttpRequest.prototype.send = function (body) {
          const cfg = window.__cp_idem_config;
          const method = (this.__cp_method || 'GET').toUpperCase();
          const url = this.__cp_url || '';
          let matchedDestructive = false;
          try {
            if (cfg && cfg.enabled) {
              const headerName = cfg.header_name || 'Idempotency-Key';
              if (
                _methodMatches(cfg, method) &&
                _endpointMatches(cfg, url) &&
                (!this.__cp_idem_already_set || cfg.allow_existing_header === false)
              ) {
                matchedDestructive = true;
                _setRH.call(this, headerName, _buildKey(cfg, method, url, body));
              }
            }
          } catch (e) {
            // F-08e: structured diagnostic + fail-closed for matched
            // destructive requests. Unmatched requests still proceed
            // (we wouldn't have augmented them anyway).
            window.__cp_idem_diagnostics.push({
              ts: Date.now(),
              where: 'xhr_send',
              error: String(e && e.message || e),
              method: method,
              url: url,
              matched: matchedDestructive,
            });
            if (matchedDestructive) {
              throw new Error(
                '[cp-idempotency] shim failed on matched destructive ' +
                method + ' ' + url + ': ' + (e && e.message || e)
              );
            }
          }
          return _send.apply(this, arguments);
        };
      }
      return true;
    }
    """

    def _set_step_idem_context(self, page: Page, step: SkillStep) -> None:
        """Set the per-step idempotency config on the page.

        WI-04: this now writes the full capability config (header
        name, endpoint patterns, method patterns, key components)
        plus the per-step context (session id + step index) so the
        page-side shim has everything it needs to build the right
        key for the right requests. When the portal doesn't declare
        idempotency capability, this still runs but writes ``enabled:
        False`` so the shim short-circuits.

        Also logs a warning if the step is annotated destructive
        (requires_gate) but no capability is configured -- that's the
        operator's signal to declare idempotency on the portal."""
        cap = getattr(self, "idempotency_capability", None)
        if cap is None or not getattr(cap, "enabled", False):
            # Even when disabled, clear stale config from a prior task
            # that may have left it set.
            try:
                page.evaluate(
                    "() => { window.__cp_idem_config = { enabled: false }; }"
                )
            except Exception:
                pass
            if step.requires_gate:
                self.audit.log(
                    "warn",
                    (
                        f"step {step.index} is destructive but portal has no "
                        "idempotency capability configured -- retries may "
                        "double-write. Declare PortalContext.idempotency to "
                        "fix."
                    ),
                    data={"step_index": step.index, "label": step.semantic_label},
                )
            return

        cfg = {
            "enabled": True,
            "header_name": getattr(cap, "header_name", "Idempotency-Key"),
            "endpoint_patterns": list(getattr(cap, "endpoint_patterns", []) or []),
            "method_patterns": list(getattr(cap, "method_patterns", []) or []),
            "key_components": list(getattr(cap, "key_components", []) or []),
            "body_hash_fields": list(getattr(cap, "body_hash_fields", []) or []),
            "allow_existing_header": bool(
                getattr(cap, "allow_existing_header", True)
            ),
            "scope_all_endpoints": bool(
                getattr(cap, "scope_all_endpoints", False)
            ),
            "session_id": self.session_id,
            "step_index": step.index,
        }
        try:
            page.evaluate(
                "(cfg) => { window.__cp_idem_config = cfg; }", cfg
            )
        except Exception as e:  # noqa: BLE001
            self.audit.log(
                "warn",
                (
                    f"idempotency config not set for step {step.index} "
                    f"({type(e).__name__}: {e}) -- retries of this step "
                    "may double-write to destructive endpoints"
                ),
            )

    def _resolve_baseline_ts(self, baseline_event_id: Optional[str]) -> int:
        """F-09d + WI-09: look up the baseline timestamp for a network
        expectation.

        Two sources, in order:
          1. Explicit ``baseline_event_id``: the recorded started-at ms
             of that event, populated when its step ran.
          2. Implicit step-start baseline (WI-09): when no
             baseline_event_id was supplied, use self._step_started_ms
             -- the moment the current step's _execute_step started.
             This closes the BLOCKER-4 stale-request hole: a request
             that finished before THIS step's action ran cannot
             satisfy this step's expected_signal.

        Returns 0 only when neither baseline is available (legacy
        skill, no recorded step start). 0 disables the started_ts
        comparison and reproduces pre-F-09 behavior."""
        if baseline_event_id:
            recorded = self._event_baseline_ts.get(baseline_event_id, 0)
            if recorded:
                return int(recorded)
        # WI-09 implicit: action-scoped baseline.
        return int(getattr(self, "_step_started_ms", 0) or 0)

    def _drain_idempotency_diagnostics(self, page: Page, step: SkillStep) -> None:
        """F-08e: pull the page-side shim's diagnostic queue and emit
        any entries as audit warnings. Called after each step so the
        operator sees shim exceptions even though the JS catch blocks
        couldn't reach the Python audit directly.

        Entries with ``matched: true`` are particularly serious -- they
        mean a destructive request that we were configured to augment
        threw inside our shim. The JS side fails closed for these (the
        page sees an error), but the diagnostic still surfaces here
        for the audit trail.
        """
        try:
            entries = page.evaluate(
                "() => { const q = window.__cp_idem_diagnostics || []; "
                "window.__cp_idem_diagnostics = []; return q; }"
            )
        except Exception:
            return
        if not entries:
            return
        for entry in entries:
            matched = bool(entry.get("matched"))
            level = "error" if matched else "warn"
            self.audit.log(
                level,
                (
                    f"idempotency shim {'failed CLOSED on matched destructive ' if matched else 'soft-failed on '}"
                    f"{entry.get('method', '?')} {entry.get('url', '?')}: "
                    f"{entry.get('error', '?')}"
                ),
                data={
                    "step_index": step.index,
                    "where": entry.get("where"),
                    "matched_destructive": matched,
                },
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
                # F-09c: when status is None, the schema docstring
                # documents "any 2xx" -- enforce that in the runner
                # predicate rather than accepting any status.
                # F-09d: when ne.started_after_event is set, the
                # matched request must have started_ts strictly
                # greater than the baseline timestamp. The runner
                # resolves baseline event_id -> ts via the per-step
                # baseline registry on self (_event_baseline_ts);
                # entries are populated when a step that produces a
                # baseline event runs. None => no baseline scoping
                # (legacy behavior).
                baseline_ts = self._resolve_baseline_ts(
                    ne.started_after_event
                )
                page.wait_for_function(
                    "([pat, method, statusFilter, baselineTs]) => {"
                    " const log = window.__cp_request_log || [];"
                    " return log.some(r =>"
                    "   (r.url || '').toLowerCase().includes(pat) &&"
                    "   (!method || (r.method || 'GET').toUpperCase() === method.toUpperCase()) &&"
                    "   (statusFilter === null ? (r.status >= 200 && r.status < 300) : r.status === statusFilter) &&"
                    "   r.finished_ts > 0 &&"
                    "   (baselineTs === 0 || (r.started_ts || 0) > baselineTs)"
                    " ); }",
                    arg=[pattern, ne.method, ne.status, baseline_ts],
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
                elif de.kind == "aria_busy":
                    # WI-10: wait for aria-busy on the target to settle
                    # to "false" (or be absent entirely). The grabber
                    # observes this transition during the originating
                    # action's effect window.
                    page.wait_for_function(
                        "([sel]) => {"
                        " const el = document.querySelector(sel);"
                        " if (!el) return false;"
                        " const v = el.getAttribute('aria-busy');"
                        " return v === null || v === 'false'; }",
                        arg=[de.selector],
                        timeout=de.timeout_ms,
                    )
                elif de.kind in ("disabled_until_enabled", "field_enabled"):
                    # WI-10: wait for the target to be NOT disabled.
                    # Honors both the HTML disabled attribute AND the
                    # aria-disabled state used by ARIA combobox widgets.
                    page.wait_for_function(
                        "([sel]) => {"
                        " const el = document.querySelector(sel);"
                        " if (!el) return false;"
                        " if (el.hasAttribute('disabled')) return false;"
                        " if (el.getAttribute('aria-disabled') === 'true')"
                        "   return false;"
                        " return true; }",
                        arg=[de.selector],
                        timeout=de.timeout_ms,
                    )
                elif de.kind == "role_progressbar_hidden":
                    # WI-10: any descendant role=progressbar inside
                    # ``selector`` must be gone or hidden.
                    page.wait_for_function(
                        "([sel]) => {"
                        " const root = document.querySelector(sel);"
                        " if (!root) return true;"
                        " const bars = root.querySelectorAll(\"[role='progressbar']\");"
                        " for (const b of bars) {"
                        "   const cs = window.getComputedStyle(b);"
                        "   if (cs && cs.display !== 'none' &&"
                        "       cs.visibility !== 'hidden') return false;"
                        " }"
                        " return true; }",
                        arg=[de.selector],
                        timeout=de.timeout_ms,
                    )
                elif de.kind == "text_transition":
                    # WI-10: wait for selector's innerText to contain
                    # the declared ``text`` (case-insensitive
                    # substring). Used for button labels that revert
                    # from "Saving..." to "Save" when complete.
                    page.wait_for_function(
                        "([sel, expected]) => {"
                        " const el = document.querySelector(sel);"
                        " if (!el) return false;"
                        " if (!expected) return true;"
                        " const t = (el.innerText || el.textContent ||"
                        "            '').toLowerCase();"
                        " return t.includes(String(expected).toLowerCase()); }",
                        arg=[de.selector, de.text or ""],
                        timeout=de.timeout_ms,
                    )
                elif de.kind == "selector_hidden":
                    # WI-10: simple hidden alias for arbitrary
                    # readiness markup the operator declares.
                    page.locator(de.selector).first.wait_for(
                        state="hidden", timeout=de.timeout_ms
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
        except Exception as e:
            # WI-06: screenshot capture used to fail silently. The
            # operator then saw a step-failed event with no
            # screenshot_path and no clue why. Surface so the
            # screenshot subsystem can be debugged separately from the
            # step's actual outcome.
            self._diagnostic(
                "runner.screenshot_failed",
                level="warn",
                recoverable=True,
                exc_type=type(e).__name__,
                exc_msg=str(e)[:200],
                label=label,
            )
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
    """Legacy helper. WI-24 replaced most call sites with semantic
    locator APIs (``get_by_test_id`` / ``get_by_label`` / etc.) or with
    the runner's ``_attr_locator`` helper. The original helper only
    escaped backslash and single quote -- IDs containing ``]``,
    double quotes, spaces, colons, or non-ASCII characters broke the
    selector. Kept here for backward compatibility with any external
    consumer; new in-tree code should use ``_attr_locator``."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _probe_locator(loc: Locator) -> LocatorProbeResult:
    """WI-23: structured probe of a Playwright locator.

    Replaces the legacy ``_first_visible`` bool helper which caught
    every exception and degraded to ``count() > 0`` -- masking real
    failures (hidden / strict-mode / detached / timeout) as "looks
    fine". The runner now branches on ``state`` so visibility failures
    never silently convert into "click this element."
    """
    # Count first; zero matches is the common fall-through case and
    # doesn't need a visibility check.
    try:
        count = loc.count()
    except PWTimeoutError as e:
        return LocatorProbeResult(
            state="timeout",
            count=0,
            last_error=f"count timeout: {e}",
        )
    except Exception as e:
        msg = str(e)
        # Playwright strict mode raises a specific class; surface as
        # strict_mode_error when we can detect the pattern.
        if "strict mode" in msg.lower() or "resolved to" in msg.lower():
            return LocatorProbeResult(
                state="strict_mode_error",
                count=0,
                last_error=msg[:240],
            )
        return LocatorProbeResult(
            state="zero_matches",
            count=0,
            last_error=msg[:240],
        )
    if count == 0:
        return LocatorProbeResult(state="zero_matches", count=0)
    # Visibility check. Use a short timeout because the caller has
    # already waited for page settle; this is a fast probe, not a
    # wait-until-visible.
    try:
        if loc.first.is_visible(timeout=500):
            if count == 1:
                return LocatorProbeResult(state="visible_unique", count=1)
            return LocatorProbeResult(state="multiple_matches", count=count)
        return LocatorProbeResult(
            state="hidden",
            count=count,
            last_error="first match is not visible",
        )
    except PWTimeoutError as e:
        return LocatorProbeResult(
            state="timeout",
            count=count,
            last_error=f"visibility timeout: {e}",
        )
    except Exception as e:
        msg = str(e)
        if "detached" in msg.lower() or "element handle" in msg.lower():
            return LocatorProbeResult(
                state="detached",
                count=count,
                last_error=msg[:240],
            )
        if "strict mode" in msg.lower():
            return LocatorProbeResult(
                state="strict_mode_error",
                count=count,
                last_error=msg[:240],
            )
        return LocatorProbeResult(
            state="hidden",
            count=count,
            last_error=msg[:240],
        )


def _first_visible(loc: Locator) -> bool:
    """Deprecated since WI-23. Wraps ``_probe_locator`` for the few
    legacy call sites that still need a boolean answer; new code should
    branch on ``_probe_locator(...).state`` directly so hidden /
    detached / strict-mode / timeout cannot silently be treated as
    'looks fine'."""
    return _probe_locator(loc).state in ("visible_unique", "multiple_matches")


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
