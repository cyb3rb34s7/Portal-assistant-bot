# CurationPilot - Structural Fix Engineering Guide

A condensed engineering guide for the next operator on the project.
Walks the data flow from grabber to runner, names every component the
2026-05-21 structural fix sprint reshaped, and cites file:line locations
the next operator can jump to.

This is a "where things live and why" doc, not API reference. For the
why-this-was-broken history, read
[`reviews/2026-05-21_teach-replay-audit.md`](reviews/2026-05-21_teach-replay-audit.md).
For the change log of what landed, read
[`reviews/2026-05-21_droid-completion-summary.md`](reviews/2026-05-21_droid-completion-summary.md).

## 1. Data flow at a glance

```
Operator demonstrates in Chrome
       |
       v
[grabber.js]  posts TraceEvents over CDP binding
       |
       v
[teach.py]    persists TraceEvent JSONL + screenshots + portal catalog
       |
       v
[annotate.py] reads JSONL, builds causality graph, detects semantic
              clusters (fill_submit / set_selection / select_option /
              autocomplete / cascading / modal / popup / drag_drop /
              date_select / scroll_until / rich_text_set / shortcut /
              toggle_state / canvas_gesture / download), emits
              SemanticCluster + SkillStep with provenance + effects +
              expected_signals + assert_after
       |
       v
[skill_models.py] validates the resulting Skill (pydantic). Legacy
              v1 JSON loads via `Skill.model_validator(mode='before')`
              upgrader (the WI-08 click+nav collapse) and the
              `pilot.skill_upgrade` module covers everything else.
       |
       v
[skill_runner.py] executes each SkillStep. Resolves locators via L1
              (test_id / element_id / name / aria_label), L2 (role +
              accessible name), then L3 (locator_repair). Dispatches
              the action through Playwright's sync API. Verifies
              expected_signals + assert_after AFTER each action.
       |
       v
[adapters/]  per-portal write/read methods for the legacy task-list
              runner AND the WI-49 canvas-gesture registry.
```

The structural fix sprint was about making each of these stages do its
ONE job. Pre-sprint, grabber decided semantics (the 400ms debounce
constituted a "fill" at the recorder layer), annotator did substring
templating ("luck-based"), and runner inferred intent at replay
("fuzzy match this value against the current options because it's
~0.7 similar"). The audit's STRATEGIC items called these
"wrong-layer assumptions" -- each WI moves semantics to the right
layer.

## 2. Schema (`pilot/skill_models.py`)

Pydantic models the rest of the package consumes. `Skill` is the top
level; everything else is reachable from it.

### Skill versioning

- `Skill.schema_version` at `pilot/skill_models.py:2974`. Default `1`
  so legacy JSON loads without the field. Bumped to `2` by the
  upgrader (`pilot.skill_upgrade.upgrade_skill_to_v2`).
- `Skill.upgraded_from` at `pilot/skill_models.py:2975`. Audit
  breadcrumb stamped during legacy upgrade; None for natively-v2
  skills.
- `Skill._wi08_upgrade` model_validator at `pilot/skill_models.py:3026`.
  Silently collapses legacy click + standalone-navigate pairs at load
  time and records the count in `legacy_nav_upgrade_count`.

### Steps

`SkillStep` at `pilot/skill_models.py:2363`. ONE step = one semantic
unit (not one raw event). Carries:

- `index: int`, `action: ActionType` (Literal at `pilot/skill_models.py:25`).
- `fingerprint: Optional[ElementFingerprint]` -- multi-locator capture
  (test_id, element_id, name, aria_label, role, accessible_name,
  text, placeholder, control_kind, value_kind, options_snapshot,
  selected_options, aria_expanded/disabled, disabled, readonly,
  contenteditable, locale_hint, timezone_hint, frame_path).
- Per-action payload: `url`, `value`, `file_path`, `wait_ms`.
- Annotator-emitted: `semantic_label`, `param_binding`,
  `expected_signals` (the WAIT contract), `assert_after`
  (`StepAssertion[]` -- the VERIFY contract).
- WI-22+ `ambiguity_policy`, WI-26 `repair_policy`,
  WI-13 `value_transition`, WI-14 `click_gesture` + `effect_signature`.
- WI-15..WI-49 typed sub-specs (`fill_submit`, `select_autocomplete`,
  `select_option`, `dependency_chain`, `date_select`, `slider_set`,
  `shortcut`, `rich_text`, `file_spec`, `drag_drop`, `download_spec`,
  `canvas_gesture`, `toggle_state`, `scroll_until`).
- WI-01 `effects: StepEffect` -- consequences of THIS action
  (navigation / popup / modal / download / toast / hover / network /
  dom / state_change), `replay_policy: ReplayPolicy` -- per-step
  fail-fast policy, `provenance: StepProvenance` -- how the
  annotator built this step.
- WI-36 `auth_precondition`, WI-46 `page_context`, WI-48
  `strict_locale`.

### Effects

`StepEffect` at `pilot/skill_models.py:870`. Composes the
sub-effect models. The annotator FOLDS consequences into the causing
step (e.g. a click that opens a dialog has `effects.modal.opens_on_action=True`),
so the runner verifies the cause produced the effect rather than
re-executing a separate "open dialog" step.

- `NavigationEffect` at `pilot/skill_models.py:602`. Tracks `kind`
  (`spa_route` | `full_document` | `hash` | `history_replace` |
  `manual`), `url` (literal observed), `url_template` (provenance-
  derived), `assert_url`, `reload_allowed: bool=False`.
- `PopupEffect` at `pilot/skill_models.py:651`. `page_binding_key`
  routes subsequent steps to `BrowserSession.popup_pages[key]`.
- `DownloadEffect` at `pilot/skill_models.py:666` +
  `DownloadSpec` at `pilot/skill_models.py:2210`. The Effect is the
  observed download intent on the step; the Spec is the step-level
  validation contract (filename_template, expected_mime, etc.).
- `ModalEffect` at `pilot/skill_models.py:696`. WI-34 contract is
  `opens_on_action` + `closes_on_action` booleans; legacy `kind`
  field is back-compat only.
- `HoverEffect` at `pilot/skill_models.py:750`. Hover that revealed a
  submenu, recorded only when the hover caused a visibility
  transition.
- `ToastEffect` at `pilot/skill_models.py:793`. Transient toasts +
  their action buttons (Undo, Retry).
- `StateChangeEffect` at `pilot/skill_models.py:859`. Catches the
  long-tail (aria-expanded change, aria-checked change, etc.).

### Expected signals + assertions

- `ExpectedSignals` at `pilot/skill_models.py:561` is the WAIT
  contract: `network: NetworkExpectation[]`, `dom: DomExpectation[]`,
  `push: PushExpectation[]` (WI-47). Runner waits for these AFTER
  the action.
- `NetworkExpectation` at `pilot/skill_models.py:444`. Per-step network
  wait. WI-09 added `started_after_event` so a stale prior request
  can't accidentally satisfy a new step. `status=None` means
  "any 2xx" per F-09d.
- `DomExpectation` at `pilot/skill_models.py:472`. `kind` covers
  `visible`, `hidden`, `options_changed`, `count_changed`, plus WI-10
  readiness kinds (`aria_busy`, `role_progressbar_hidden`,
  `disabled_until_enabled`, `field_enabled`, `text_transition`,
  `selector_hidden`) and WI-38 `target_visible_in_scroller`.
- `PushExpectation` at `pilot/skill_models.py:516`. WI-47 channel
  substring + optional type/payload substring. Runner inspects
  page-side `__cp_request_log` for WS / SSE entries.
- `StepAssertion` at `pilot/skill_models.py:353` is the VERIFY contract.
  WI-27 expanded kinds: `url_matches_template`, `field_value_equals`,
  `selection_equals`, `toast_visible`, `request_completed`,
  `download_started`, `validation_field` (WI-44). Each carries the
  fields it needs (`url_template`, `request_pattern`,
  `validation_field_id`, etc.).

### Provenance

WI-11 replaced "substring template luck" with declared sources.

- `ParamProvenance` at `pilot/skill_models.py:1007`. `source` Literal
  is `operator_input | csv_row | csv_column | trace_recorded |
  route_param | request_param | selected_option | file_metadata |
  row_key | ancestor_attribute`. Carries `source_step`,
  `source_attribute`, `confidence`. `request_param` covers both
  query-string and body-derived templates; the producer
  (`_derive_provenance_templates`) does not currently distinguish
  the two sub-sources, so they share one Literal value. If a future
  WI needs to differentiate query vs body for replay reasoning,
  split the Literal and update the producer + tests in lockstep.
- `TemplatePart` at `pilot/skill_models.py:310`. A list of these
  composes a parameterized string (selector / URL). Replaces the
  raw-string substring-replace pass.
- `StepProvenance` at `pilot/skill_models.py:1032`. Per-step audit of
  which raw events composed the step, what cluster_kind detected it,
  and the alternatives the detector evaluated.

### Replay policy

- `ReplayPolicy` at `pilot/skill_models.py:910`. WI-07. Per-step
  `on_failure: 'abort' | 'continue' | 'optional' | 'recover'`,
  default `abort`. F-09a normalizes `optional=True` + `abort` -> `optional`.
- The runner reads `step.replay_policy.on_failure` and breaks the
  loop on abort/recover -- pre-WI-07 it kept going.

### Trace events

`TraceEvent` at `pilot/skill_models.py:3127`. ONE raw event from the
grabber, persisted into the session's JSONL.

- WI-02 identity + causality + ordering: `event_id`, `interaction_id`,
  `caused_by`, `sequence`, `source`, `monotonic_ts`, `raw_event_kind`.
- F-06 page state: `page_state_before`, `page_state_after`.
- WI-03 element metadata is on the `fingerprint` not the event itself.
- WI-13 `value_before`. WI-14 `click_detail`, `pointer_type`,
  `target_state_before`, `target_state_after`.
- F-07 observed kinds: `network_request`, `network_response`,
  `dom_mutation`, `popup`, `download`, `visibility_change`.
- WI-29 `file_metadata`. WI-30 `drop_target_fp`,
  `data_transfer_summary`. WI-34 `dialog_state` / `dialog_selector` /
  `dialog_aria_modal`. WI-35 `popup_url` etc. WI-38 scroll payload.
  WI-39 rich-text payload. WI-40 hover payload. WI-41 shortcut payload.
  WI-42 toast payload. WI-45 download payload.

## 3. Grabber (`pilot/overlay/grabber.js`)

Injected at teach time into every page (and into same-origin iframes
when WI-32's chain captures the path). Posts `TraceEvent` shapes back
over a Runtime binding installed by `pilot/teach.py`.

Key responsibilities (each is one of the audit's "wrong-layer"
corrections):

1. **Capture raw transitions, not semantic units.** WI-12 / WI-15 /
   WI-19 / WI-21 / WI-28 / WI-30 / WI-39 / WI-41 each rely on the
   grabber posting EVERY input_change / pointerdown / keydown / etc.
   so the annotator can collapse them. The 400ms debounce that was
   pre-sprint policy is gone for semantic-collapse purposes; some
   grabber-side coalescing remains for raw transport safety only.

2. **Explicit causal tokens, not time windows.** F-08a established
   `window.__cp_active_interaction = {id, ...}` set INSIDE the
   synchronous click handler. The `history.pushState` wrapper /
   fetch wrapper / etc. read this synchronously when emitting their
   own events, so a navigate that fires after the click-handler-
   spawned microtask carries `caused_by = click.id`. Background
   pollers that fire later have `caused_by = null`.

3. **Observed events as TraceEvents, not counters.** F-07. The fetch
   and XHR hooks emit `kind='network_request'` + `kind='network_response'`
   with `request_id` linking them. MutationObserver bursts emit
   `kind='dom_mutation'` with a debounced summary. The `__cp_inflight`
   counter still exists for the legacy "any in-flight requests?" wait
   but the WI-09 path uses the event stream.

4. **Page state snapshots.** F-06. Each user-action event carries
   `{url, title, key_dom_signature}` before + after. `key_dom_signature`
   is a cheap hash of the target ancestor chain's BoundingClientRect
   so the annotator can detect "this isn't the same page state."

5. **Element metadata at record.** WI-03. Selects record full
   `OptionSnapshot[]`, ARIA combobox/listbox roles record
   `aria_expanded` + `aria_disabled`, contenteditables record
   `contenteditable=true`, date inputs record `locale_hint` +
   `timezone_hint` from `Intl.DateTimeFormat().resolvedOptions().timeZone`.

6. **Idempotency shim.** The grabber doesn't install it; the runner
   does. But the grabber's fetch wrapper detects when a portal-app
   per-call key is being generated and tags the request so the
   runner can decide whether to override per `allow_existing_header`.

The grabber's defaults that remain (F-08-flagged):

- `options_snapshot_max=500` (was `200`); when hit, the fingerprint's
  `options_truncated=True` and the annotator suggests a search step.
  Configurable via `PortalContext.wait_policy.options_snapshot_max`
  (`pilot/agent/schemas/portal_context.py:80`).
- DOM mutation debounce window is bounded to bound payload size.
  Documented inline as a transport bound, not a semantic decision.

## 4. Annotator (`pilot/annotate.py`)

`run_annotate` is the entry point. The order of operations is
load-bearing:

1. Read TraceEvents from session JSONL.
2. `_assign_synthetic_ids` (`pilot/annotate.py:~71`) fills any
   missing event_ids for legacy traces.
3. `build_causality_graph` (~`pilot/annotate.py:99`) constructs a DAG
   from `caused_by` / `interaction_id` / `sequence`. **This must run
   BEFORE `filter_events` -- F-05.**
4. `filter_events` removes legacy heuristic-flagged noise. Pre-sprint
   this dropped same-URL navigations, empty input changes, duplicate
   clicks <200ms. WI-08 / WI-13 / WI-14 turned each of those into
   semantic features (causality-aware nav, `ValueTransition`,
   `click_gesture`); the filter still runs for transport safety but
   the graph upstream knows the causal facts the filter would have
   otherwise hidden.
5. `build_skill` walks the graph + filtered events to:
   - **Detect semantic clusters** -- one `SemanticCluster` per
     widget interaction. `cluster_kind` is the catalog at
     `pilot/skill_models.py:1113` (click_with_navigation, fill_submit,
     select_option, select_autocomplete, cascading_select,
     set_selection, modal_open / modal_close, toggle_state,
     date_select, slider_set, drag_drop, rich_text_set,
     download_click, popup_open, scroll_until).
   - **Bind params + provenance** -- map operator-typed values onto
     `SkillParam` declarations; mark each binding's source
     (`row_key` / `route_param` / `request_param` / `selected_option`
     / `operator_input`).
   - **Materialize each cluster as a SkillStep** with provenance,
     effects (folded consequences), and expected_signals.
   - **Emit assertions** (WI-27) for action-specific postconditions.
6. LLM enrichment (WI-31, `pilot/agent/annotate_llm.py`) gates ON
   structural overlays: the LLM can confirm a detected cluster or
   rename params, but cannot ADD a dependency without supporting
   trace evidence. Validation lives at the boundary -- the LLM's
   JSON output is parsed into structured types and rejected if it
   contradicts the deterministic pass.

Annotator design rules to keep:

- **Decide semantics here, not at replay.** The runner just executes.
- **Templates from provenance, not luck.** A `btn-open-A-9001` test_id
  becomes `btn-open-{asset_id}` only when the asset_id source is
  declared (row_key from the catalog page, request_param from the
  preceding search, etc.). Substring templates remain in the codebase
  as a last-resort legacy fallback and emit `template_source = legacy_substring`
  on the provenance.
- **Required signals must be required.** If a region/market cascade
  was observed during recording, the annotator emits
  `ExpectedSignals.network` with `started_after_event` pointing at the
  parent select's event_id. The runner FAILS the step when the signal
  doesn't arrive within timeout; pre-sprint it just kept going.

## 5. Runner (`pilot/skill_runner.py`)

`SkillRunner.run()` is the main loop. The per-step contract is now:

1. **Pre-step**: locator resolution (L1 -> L2 -> alternates -> L3
   repair). WI-22 ambiguity policy fires when candidate count exceeds
   `expected_candidate_count`. WI-23 `LocatorProbeResult`
   (`pilot/models.py:85`) gives structured states (visible_unique /
   zero_matches / multiple_matches / hidden / detached /
   strict_mode_error / timeout) instead of the legacy boolean.
2. **Dispatch**: based on `step.action`, the runner calls per-action
   methods (`_do_click`, `_do_fill_submit`, `_do_select_option`,
   `_do_select_autocomplete`, `_do_set_selection`, `_do_date_select`,
   `_do_slider_set`, `_do_drag_drop`, `_do_toggle_state`,
   `_do_rich_text_set`, `_do_shortcut`, `_do_modal`, `_do_popup`,
   `_do_download`, `_do_scroll_until`, `_do_canvas_gesture`). Each
   reads its typed sub-spec off the SkillStep (e.g.
   `step.fill_submit: FillSubmitSpec`).
3. **Post-step**: wait for `step.expected_signals` (WI-09 action-scoped
   baselines), verify `step.assert_after` (WI-27 action-specific
   postconditions). Failure of either is a structured `error_kind`
   on the `ToolResult`.
4. **Policy**: read `step.replay_policy.on_failure`. Default `abort`
   stops the runner; orchestrator pause/retry/skip flow takes over.

### Codecs

WI-05 + WI-48 typed params. `pilot/param_codecs.py` (referenced from
`SkillParam.codec`) holds `iso_date`, `iso_datetime`,
`localized_number`, `enum_value`, `enum_label`, `file_ref`, `boolean`,
`raw`. Resolution happens BEFORE the action so an out-of-range value
fails fast.

### Page context

WI-46. `step.page_context.page_binding_key` selects which page
(`BrowserSession.popup_pages[key]`) the action dispatches on. The
runner maintains the registry; the annotator populates the key based
on which step is inside the popup workflow.

### Idempotency shim

Installed once per page via `_install_idempotency_shim` (the JS lives
inline starting around `pilot/skill_runner.py:5640`). Wraps `fetch` +
`XMLHttpRequest.send`. Reads `cfg.header_name` from the portal context
(F-03). Detects `input instanceof Request` and seeds augmentation from
`Request.headers` (F-04). Body hash includes method + canonical URL +
configured body fields per WI-04.

The shim writes structured diagnostics into `window.__cp_idem_diagnostics`
which the runner drains after each step so any swallowed exception
becomes a visible audit event (F-08e).

## 6. Portal context (`pilot/agent/schemas/portal_context.py`)

YAML at `portals/<portal_id>/context.yaml` parses into `PortalContext`
at `pilot/agent/schemas/portal_context.py:206`. The fields the
structural fix sprint added or made load-bearing:

- `wait_policy: WaitPolicy` at `:48`. Per-portal default wait
  timeouts. Replaces the prior pattern of class-level defaults on
  StepEffect / NetworkExpectation / etc. (F-08c).
- `auth_signal: AuthSignal` at `:223`. Pre-flight reads this to
  determine "is the portal authenticated right now?" Used by WI-36
  AuthPrecondition.
- `capabilities.idempotency: IdempotencyCapability` at `:96`. Replaces
  the prior global injection. F-08d requires non-empty
  `endpoint_patterns` when `enabled=True`.
- `external_llm_enabled` per-portal kill switch for cloud LLM calls.

## 7. Adapters (`pilot/adapters/`)

Two distinct registries (`pilot/adapters/__init__.py`):

- **Legacy portal adapters** (`BaseAdapter`, `MediaAssetsAdapter`).
  Deterministic write/read methods for ENTIRE portal pages. Used by
  the pre-sprint task-list runner (`pilot/runner.py`) and the MCP
  layer. Each adapter ships per portal.
- **WI-49 canvas-gesture adapters** (`CanvasAdapter`,
  `CanvasNoopClickAdapter`, `get_canvas_adapter`,
  `register_canvas_adapter`). Realize structured intents (e.g.
  `{kind: 'draw_box', coords: [x,y,w,h]}`) as pointer events on a
  specific canvas library. The runner looks up the adapter by
  `step.canvas_gesture.adapter_name`. Unknown adapter ->
  `error_kind='action_not_implemented'` with the missing name in
  `error_details`.

Adapters are kept in separate registries so registering a canvas
adapter doesn't pollute the portal-wide read/write API, and portals
can ship a canvas adapter without a `BaseAdapter` subclass at all.

## 8. Diagnostics (`pilot/models.py:130`)

`Diagnostic` (WI-06). Surfaces previously-silent failure sites. Stable
code convention:

- `teach.*` -- recorder-side: `teach.bad_payload_json`,
  `teach.invalid_fingerprint`, `teach.screenshot_failed`,
  `teach.snapshot_drop`.
- `runner.*` -- replay-side: `runner.watcher_install_failed`,
  `runner.idem_shim_exception`, `runner.set_selection_swallowed`.
- `annotator.*` -- annotation-side: `annotator.unmatched_navigate`,
  `annotator.dropped_clear_intent`.
- `executor.*` -- orchestrator-side: `executor.hint_persist_failed`,
  `executor.alternate_persist_failed`.

`level` is `warn` (non-fatal) or `error` (fatal). The replay UI's
`PausedModal` / `LogPane` renders these so the operator sees them in
real time rather than only in audit logs.

## 9. Upgrader (`pilot/skill_upgrade.py`)

WI-50. Stand-alone module. Pure dict-in / dict-out. Idempotent.

- `upgrade_skill_to_v2(skill_json: dict) -> dict`. Stamps
  `schema_version=2` + `upgraded_from=<original>`. Adds missing
  optional fields with safe defaults. Preserves legacy
  continue-on-failure (`replay_policy.on_failure='continue'`) so
  pre-WI-07 flows don't start aborting mid-stream.
- `upgrade_skill_file(path: Path) -> dict`. Reads, upgrades, writes
  back.
- CLI: `python -m pilot upgrade-skill <path>`.

Re-upgrading a v2 skill is a no-op; safe to run unconditionally
from CI / load hooks. Tests live at `tests/agent/test_skill_upgrade.py`.

## 10. Tests

- `tests/agent/test_skill_models.py` -- schema roundtrip + foundation
  drift fixes.
- `tests/agent/test_<wi>.py` -- one file per WI with rich scenarios.
- `tests/agent/test_wi_regression_matrix.py` -- one minimum-acceptance
  test per WI + foundation drift; the single-source "every WI has a
  guard" statement.
- `tests/agent/test_skill_upgrade.py` -- WI-50 upgrader.

Run the suite (groq-live test excluded since it requires network +
API keys):

```
.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/agent/test_groq_live.py
```

Target: 499 passing at the end of the structural fix sprint.

## 11. Where to look next

If you're adding a new interaction kind:

1. Add the cluster_kind to `SemanticCluster.cluster_kind` docstring at
   `pilot/skill_models.py:1113`.
2. Add the action to `ActionType` at `pilot/skill_models.py:25`.
3. Add the sub-spec model below `SetSelectionSpec` at
   `pilot/skill_models.py:2258` (alphabetical-ish convention; check
   what's there). Wire it into `SkillStep` at
   `pilot/skill_models.py:2363`.
4. Add grabber capture in `pilot/overlay/grabber.js` -- emit the
   right TraceEvent shape.
5. Add annotator detection in `pilot/annotate.py` -- a detector
   function consumes the causality graph + filtered events and
   returns a `SemanticCluster`.
6. Add runner dispatch in `pilot/skill_runner.py` -- a `_do_<action>`
   method that reads `step.<sub_spec>` and calls Playwright.
7. Add at least one acceptance test under `tests/agent/`.
8. Add a one-line row to `tests/agent/test_wi_regression_matrix.py`.

If you're tuning portal-specific behavior:

- Wait timeouts -> `portals/<id>/context.yaml`'s `wait_policy:` block.
- Idempotency -> `portals/<id>/context.yaml`'s
  `capabilities.idempotency:` block.
- Auth -> `portals/<id>/context.yaml`'s `auth_signal:` block.
- Canvas gestures -> register an adapter via
  `pilot.adapters.register_canvas_adapter`.

If you're debugging a flaky replay:

- Check the structured diagnostics on the `ToolResult.error_kind` /
  the orchestrator's diagnostic event stream BEFORE looking at logs.
- The runner's `error_kind` namespace is fixed: `ambiguous_target`,
  `row_not_found`, `option_not_available`, `post_condition_failed`,
  `action_not_implemented`, `dependent_option_unavailable`,
  `locator_not_visible`, `locator_zero_match`,
  `locator_multiple_matches`, `auth_missing`, `locale_mismatch`,
  `download_constraint_failed`, `validation_field_failed`.
- Each `error_kind` carries `error_details` with the structured
  context (candidate list for ambiguous, available options for
  option_not_available, etc.). The Replay UI's row picker renders
  these natively; the CLI logs them as JSON.

## 12. Sequencing notes for future structural work

- Schema changes propagate: producer (grabber) -> schema
  (skill_models) -> consumer (annotator + runner). Don't add a
  field unless all three are wired -- the prior agent's drift was
  schema-then-forget.
- Default values need a justification or a configurable home.
  Hardcoded literals in `*_models.py` get caught by the next audit;
  prefer pulling them from `PortalContext.wait_policy`.
- The runner is sync; the orchestrator is async. `playwright.sync_api`
  is thread-affine -- new threads break it.
- `python -m pilot serve` runs the FastAPI UI on `5177`. Tests assume
  this is NOT running.
