# CurationPilot Teach / Annotate / Replay — Full Implementation Plan

I read both required audit documents and re-checked the relevant code paths: `pilot/overlay/grabber.js`, `pilot/teach.py`, `pilot/annotate.py`, `pilot/skill_models.py`, `pilot/skill_runner.py`, `pilot/agent/locator_repair.py`, `pilot/agent/executor_real.py`, orchestrator/UI pause handling, and the sample portal.

## Pushback / Design Corrections

- The prior “correlate click with navigation in grabber” recommendation was directionally right but incomplete: we should **not suppress raw events**. We should record causality and let annotate collapse into replayable semantic steps.
- `expected_signals` should be produced deterministically from captured request/DOM evidence first; LLM can enrich names, but should not be the source of structural truth.
- Fuzzy select and L3 repair thresholds should not become “configurable magic numbers.” The better design is **declared aliases + uniqueness + postconditions**.
- Fail-fast must be enforced inside `SkillRunner`; orchestrator pause/retry/skip remains the operator recovery surface, but the runner must not keep mutating after a failed sub-step.
- The current v2 LLM sidecar enriches planning metadata only. It must become a structural enrichment layer, but only after deterministic annotate has built the core shape.

---

# Foundation / Schema Work

## WI-01. Add schema versioning for structural replay semantics

Source: BLOCKER 1, BLOCKER 2, BLOCKER 3, STRATEGIC item 3, all downstream interaction rows.

Why this ordering: This must land before grabber, annotator, or runner changes because every later item needs a common vocabulary for event causality, action effects, typed widget semantics, waits, assertions, and migration. The rejected alternative is to add one-off fields per bug, which would make runner behavior keep re-deriving semantics from heuristics. This prevents the failure mode where semantically different interactions are all replayed as generic `click` / `change`.

Files to touch:
- `pilot/skill_models.py:25-450`
- `pilot/agent/schemas/skill.py`
- `pilot/annotate.py:195-266`
- `pilot/skill_runner.py:227-502`
- `tests/agent/`

Skill schema changes:
- Add `schema_version` handling for new step semantics while keeping legacy v1 loadable.
- Extend `ActionType` with structured actions: `fill_submit`, `select_option`, `select_autocomplete`, `set_selection`, `date_select`, `slider_set`, `drag_drop`, `toggle_state`, `modal`, `popup`, `download`, `scroll_until`, `rich_text_set`, `shortcut`, `canvas_gesture`.
- Add `StepEffect` model with `navigation`, `popup`, `download`, `modal`, `toast`, `network`, `dom`, `new_tab`, and `state_change`.
- Add `replay_policy` fields: `on_failure`, `reload_allowed`, `requires_current_page`, `optional`.
- Add `provenance` fields on params and steps.

Grabber changes:
- None directly in this item; later grabber work will populate the new fields through raw `TraceEvent` additions.

Annotator changes:
- Teach annotator to emit new schema fields only when detected.
- Preserve old skill generation path for legacy traces.

Runner changes:
- Add dispatch stubs for new action types that fail explicitly until their work item implements execution.
- Reject unknown new fields only if they cannot be safely ignored.

Migration:
- Non-breaking. Legacy skills default to existing behavior.
- Add an upgrader that fills missing `schema_version`, leaves unknown semantic fields empty, and preserves legacy `steps`.

Acceptance check:
- Existing `skills/change_title.json` loads without schema errors.
- A synthetic skill containing every new action type validates.
- Legacy unit tests still pass after loading and dumping skills.

---

## WI-02. Introduce raw trace event identity, causality, and ordering metadata

Source: BLOCKER 1, MAJOR row “Consecutive same-URL navigate dedupe uses `last_nav_url`”, STRATEGIC item 1.

Why this ordering: Causality must be captured before annotation can collapse click-navigation, popup, modal, autocomplete, or widget clusters. The rejected alternative is time-window inference in annotate only; that cannot distinguish a click-caused SPA route from a manually typed URL or router redirect. This prevents losing operator intent when multiple events happen close together.

Files to touch:
- `pilot/skill_models.py:426-450`
- `pilot/overlay/grabber.js:117-130`
- `pilot/teach.py:188-263`
- `pilot/annotate.py:70-101`

Skill schema changes:
- Extend `TraceEvent` with `event_id`, `interaction_id`, `caused_by`, `sequence`, `source`, `monotonic_ts`, `raw_event_kind`, `page_state_before`, `page_state_after`.

Grabber changes:
- Generate stable IDs for user interactions.
- Attach route changes, popups, downloads, DOM mutations, and request observations to active interactions.
- Continue posting standalone raw events instead of dropping them.

Annotator changes:
- Replace adjacency/time-only inference with causality graph construction.
- Keep raw events available for audit and migration.

Runner changes:
- None directly; later runner items consume collapsed semantic steps.

Migration:
- Existing traces without IDs are assigned synthetic IDs during annotation based on file order.

Acceptance check:
- A click followed by SPA route change records two raw events sharing causality.
- A manual URL navigation records `caused_by = null`.
- Annotator unit test verifies raw event ordering remains stable.

---

## WI-03. Capture richer element/control metadata at record time

Source: MAJOR row “Param inference only for `input_change` / `file_selected`”, 17-matrix rows 2–9, What You Missed items 47, 52, 57.

Why this ordering: Typed params and widget-specific replay require knowing the control type, options, labels, disabled/readiness state, locale, and value shape at the moment of recording. The rejected alternative is to inspect the page only at replay, but replay may have different state. This prevents mis-modeling dates, booleans, lists, sliders, contenteditable, and dynamic options as plain strings.

Files to touch:
- `pilot/overlay/grabber.js:299-321`
- `pilot/skill_models.py:38-90`
- `pilot/teach.py:188-263`
- `pilot/annotate.py:164-181`

Skill schema changes:
- Add `control_kind`, `value_kind`, `options_snapshot`, `selected_options`, `aria_expanded`, `aria_disabled`, `disabled`, `readonly`, `contenteditable`, `locale_hint`, `timezone_hint`.

Grabber changes:
- Enrich fingerprints for `select`, `input[type=date]`, `range`, checkbox/radio, contenteditable, ARIA combobox/listbox, disabled controls.
- Capture option value/text pairs for selects and ARIA listboxes.
- Capture current enabled/disabled state.

Annotator changes:
- Use `control_kind` and `value_kind` for typed params and semantic action detection.

Runner changes:
- Materialize locators with the enriched fingerprint but avoid replay-time guessing when schema has explicit type info.

Migration:
- Legacy fingerprints get `control_kind = null`; annotator falls back to old inference.

Acceptance check:
- Recording sample portal asset form includes options for `select-region`, `select-market`, `select-language`, disabled state, and `input-publish-at` date metadata.

---

## WI-04. Move idempotency injection into portal capability data

Source: MAJOR row “Idempotency key strips query/hash only”, STRATEGIC item 4.

Why this ordering: Fail-fast and retry behavior must be safe before the runner starts pausing/retrying more aggressively. The rejected alternative is improving the global key format while still injecting on every non-GET request; that still assumes backend semantics. This prevents accidental dedupe of distinct writes or injection into portals that reject unknown headers.

Files to touch:
- `pilot/agent/schemas/portal_context.py:74-120`
- `pilot/agent/executor_real.py`
- `pilot/skill_runner.py:1390-1455`
- `sample_portal/src/lib/api.js`
- `sample_portal/mock_backend.js:195-210`

Skill schema changes:
- Add portal capability model: `idempotency.enabled`, `header_name`, `endpoint_patterns`, `method_patterns`, `key_components`, `body_hash_fields`, `allow_existing_header`.

Grabber changes:
- Capture observed state-changing endpoint shapes during teach for annotator suggestions.

Annotator changes:
- Mark destructive or state-changing steps with endpoint provenance.
- Suggest portal idempotency config when sample portal exposes compatible behavior.

Runner changes:
- Inject keys only when portal capability allows it.
- Include method, canonical URL, selected query/body semantic fields, and step identity.
- Log when a destructive step runs without configured idempotency.

Migration:
- Existing global injection becomes opt-in. Sample portal context gets explicit capability data.

Acceptance check:
- Same endpoint with different request body produces different idempotency keys.
- A portal context with idempotency disabled sends no injected header.
- Sample portal retries still dedupe the exact same mutation.

---

## WI-05. Implement typed parameter inference and value codecs

Source: MAJOR row “Param inference only for `input_change` / `file_selected`”, 17-matrix rows 3, 7, 8, 9, What You Missed item 57.

Why this ordering: Semantic actions need typed params before coalescing can safely combine events. The rejected alternative is preserving all values as strings and converting in runner; that pushes business meaning into replay and breaks locale/date/number handling. This prevents selecting wrong enum values, mis-parsing dates, and passing file names where file paths are required.

Files to touch:
- `pilot/skill_models.py:90-108`
- `pilot/annotate.py:164-181`
- `pilot/agent/annotate_llm.py:35-88`
- `pilot/agent/schemas/skill.py:16-34`

Skill schema changes:
- Expand param types: `boolean`, `enum`, `string_list`, `number_range`, `date`, `datetime`, `file_path`, `object`.
- Add `codec`: `raw`, `iso_date`, `localized_number`, `enum_value`, `enum_label`, `file_ref`.
- Add `constraints`: allowed values, min/max, MIME/extension, required shape.

Grabber changes:
- Provide raw value plus display value for controls.
- Capture option labels and values.

Annotator changes:
- Infer type from control metadata and value.
- Create typed `SkillParam` declarations.
- Mark dependent enum params for current-option selection.

Runner changes:
- Resolve params through codecs before action execution.
- Fail with typed validation errors before touching the page.

Migration:
- Legacy `string` and `file_path` remain valid.
- Existing `string_list` remains valid and maps to new list codec.

Acceptance check:
- Date input records as `date`, slider as `number_range`, checkbox as `boolean`, native select as `enum`, multiselect as `string_list`.

---

## WI-06. Replace silent failures with structured diagnostics

Source: MINOR 23, BLOCKER 5, silent sites in `teach.py`, `skill_runner.py`, `executor_real.py`.

Why this ordering: Diagnostics must exist before deeper behavior changes; otherwise new structural failures will be invisible to the UI. The rejected alternative is relying on audit logs only, which operators do not see during replay. This prevents “nothing happened” failures when payload parsing, screenshots, watcher install, catalog merge, ambiguity detection, or persistence silently fail.

Files to touch:
- `pilot/teach.py:188-286`
- `pilot/skill_runner.py:1371-1380`, `623-688`, `933-954`, `1912-1916`
- `pilot/agent/executor_real.py:550-641`
- `pilot/agent/orchestrator.py:801-925`
- `curationpilot-app/src/routes/ReplayPage.tsx:771-920`
- `curationpilot-app/src/protocol/types.ts`

Skill schema changes:
- Add diagnostic fields where needed: `diagnostic_code`, `diagnostic_context`, `recoverable`.

Grabber changes:
- Emit capture transport errors as debug diagnostics when possible.

Annotator changes:
- Surface invalid trace events with counts and reasons.

Runner changes:
- Convert swallowed exceptions to structured `ToolResult.error_kind`.
- Emit warnings for non-fatal diagnostic conditions.

Migration:
- Non-breaking. Existing logs remain; new structured events supplement them.

Acceptance check:
- Simulated bad payload, screenshot failure, watcher install failure, and hint persistence failure all produce visible diagnostics.

---

## WI-07. Make runner fail-fast by default with explicit continue policy

Source: BLOCKER 5.

Why this ordering: This should land before implementing more state-changing interactions, because continuing after a failed sub-step can mutate the wrong asset. The rejected alternative is leaving continuation to callers; `SkillRunner.run()` is the unit executing sub-steps and must own safety. This prevents a stale navigation or failed locator from being followed by save/publish actions.

Files to touch:
- `pilot/skill_models.py:271-320`
- `pilot/skill_runner.py:149-178`
- `pilot/agent/executor_real.py:520-641`
- `pilot/agent/orchestrator.py:801-925`

Skill schema changes:
- Add `on_failure: "abort" | "continue" | "optional" | "recover"` on `SkillStep`.
- Default to `abort`.

Grabber changes:
- None.

Annotator changes:
- Mark only explicitly optional observational steps as `continue` or `optional`.

Runner changes:
- Stop skill execution on first failed non-optional step.
- Return partial results and failed step details.
- Preserve orchestrator pause/retry/skip as the recovery surface.

Migration:
- Legacy skills default to fail-fast.
- If necessary, migration can mark legacy `submit` duplicate events as optional after coalescing.

Acceptance check:
- A synthetic skill with failed step 2 never executes step 3 unless step 2 declares `on_failure = "continue"`.

---

# Navigation / Wait Semantics

## WI-08. Collapse click-caused SPA navigation into a click effect, not standalone `goto`

Source: BLOCKER 1, MAJOR rows “Consecutive same-URL navigate dedupe”, “Navigate URL is never templated”, “`page.goto(...networkidle...)`, then second goto”, 17-matrix row 42.

Why this ordering: This depends on WI-01 and WI-02 because it needs causal event IDs and navigation effects. It must precede URL templating and route assertions because standalone navigate replay is the core wrong-asset bug. The rejected alternatives are dropping navigate-after-click by timing or skipping duplicate URL navigates; both lose intent and still reload SPAs. This prevents clicking asset A but then navigating/reloading to asset B or remounting the same SPA route.

Files to touch:
- `pilot/overlay/grabber.js:355-378`, `625-650`
- `pilot/annotate.py:70-101`, `195-266`
- `pilot/skill_models.py:271-320`
- `pilot/skill_runner.py:307-328`, `339-359`
- `sample_portal/src/pages/Catalog.jsx:143-145`
- `tests/agent/`

Skill schema changes:
- Add `effects.navigation.kind`: `spa_route`, `full_document`, `hash`, `history_replace`, `manual`.
- Add `effects.navigation.url`, `url_template`, `source`, `reload_allowed`, `assert_url`.
- Add `standalone_navigation_reason` for true independent navigations.

Grabber changes:
- Track active click/keyboard/form interaction.
- Attach `caused_by` to History API, popstate, hashchange, and document navigations.
- Preserve raw navigation events but mark causality.

Annotator changes:
- Fold caused navigation into the causing step.
- Emit standalone `navigate` only for uncased/manual initial navigation.
- Do not use `last_nav_url` as a filter.

Runner changes:
- For click with navigation effect: click, wait/assert URL/route, never call `page.goto`.
- For standalone navigate: one navigation attempt, no second reload fallback; wait through explicit signals.

Migration:
- Existing skills with click followed by navigate can be upgraded by detecting adjacent URL causality from old traces where possible.
- If no trace exists, migration flags possible click+navigate pairs for review instead of guessing.

Acceptance check:
- Replay search for `A-9003` clicks `btn-open-A-9003`, ends on `/asset/A-9003`, and never issues `page.goto("/asset/A-9001")`.

---

## WI-09. Rebuild waits around action-scoped baselines and required signals

Source: BLOCKER 2, BLOCKER 4, MAJOR rows “Generic settle numbers”, “Request log cap 50”, What You Missed item 56.

Why this ordering: Navigation and cascading controls need reliable post-action waits. This must land before widget actions that depend on network/DOM readiness. The rejected alternative is tuning the existing generic quiet/DOM/spinner constants; that still lets stale requests and same-count option lists falsely satisfy. This prevents selecting from stale dropdown options or proceeding while the page is still processing the previous action.

Files to touch:
- `pilot/skill_models.py:143-190`
- `pilot/overlay/grabber.js:36-113`
- `pilot/skill_runner.py:1285-1698`
- `pilot/agent/schemas/portal_context.py:104-113`
- `tests/agent/`

Skill schema changes:
- Extend `ExpectedSignals` with `required`, `started_after_event`, `baseline`, `match_mode`, `body_predicate`, `option_signature`, `request_id`.
- Add `WaitPolicy` per portal/step.

Grabber changes:
- Capture request start/finish with IDs, method, URL, status, initiator interaction, and response timing.
- Capture option signatures before/after interactions when option lists change.

Annotator changes:
- Attach network and DOM expectations to the action that caused them.
- Use request baselines rather than whole-log substring search.

Runner changes:
- Move `expected_signals` wait to after the action.
- Fail required misses.
- Use action baseline timestamps/request IDs.
- Replace global ring buffer with action-scoped request registry.
- Compare option identity signatures, not just counts.

Migration:
- Legacy skills without expected signals use safer generic waits, but required structural waits are absent.
- Re-annotation from trace can populate signals when raw request logs exist.

Acceptance check:
- Region change waits for the matching `/api/markets?region=...` request after the change and for `select-market` option signature to change.
- A stale prior request cannot satisfy a later step.

---

## WI-10. Replace spinner conventions with observed busy/readiness signals

Source: MAJOR row “Spinner selector list is convention-based”, BLOCKER 4, What You Missed item 52.

Why this ordering: This extends WI-09 and feeds readiness into later widget work. The rejected alternative is adding more spinner selectors; enterprise portals use arbitrary markup. This prevents acting while save/search/apply buttons are disabled or while non-standard loading UI is still active.

Files to touch:
- `pilot/overlay/grabber.js:535-613`
- `pilot/annotate.py`
- `pilot/skill_models.py:161-190`
- `pilot/skill_runner.py:1261-1268`, `1492-1556`
- `sample_portal/src/pages/AssetDetail.jsx:335-378`

Skill schema changes:
- Add `BusyExpectation`: `aria_busy`, `role_progressbar`, `disabled_until_enabled`, `text_transition`, `selector_hidden`, `field_enabled`.
- Add `readiness_assertions`.

Grabber changes:
- Observe `aria-busy`, disabled attributes, progressbar visibility, and button text transitions during action effects.

Annotator changes:
- Convert observed loading/readiness changes into expected DOM signals.
- Prefer field/button readiness over spinner conventions.

Runner changes:
- Wait for declared readiness signals.
- Use spinner selector only as legacy fallback.

Migration:
- Existing skills keep fallback behavior.
- Re-annotation can add readiness signals from raw trace.

Acceptance check:
- Save button disabled/enabled transition is captured and replay waits until save completion without relying on `status-saving` naming.

---

# Annotator Core Semantics

## WI-11. Replace substring template luck with provenance-based templates

Source: MAJOR rows “Template derivation uses substring replace, `len >= 3`”, “Navigate URL is never templated”, STRATEGIC item 2.

Why this ordering: It depends on typed params and causality, and it must precede autocomplete/table/navigation correctness. The rejected alternative is tuning substring length or using regex replacement; that still produces `btn-open-{input_catalog_search}01` from `A-90` inside `A-9001`. This prevents wrong locators and literal route replay.

Files to touch:
- `pilot/annotate.py:270-340`
- `pilot/skill_models.py:38-90`, `90-108`
- `pilot/skill_runner.py:1069-1096`
- `tests/agent/test_fingerprint_templating.py`

Skill schema changes:
- Add `ParamProvenance`: source step, source attribute, row key, route param, request param, selected option, file metadata.
- Add `TemplatePart` model instead of raw string-only templates.
- Add URL templates under navigation effects and standalone navigates.

Grabber changes:
- Capture row/ancestor key attributes and request params where possible.

Annotator changes:
- Derive templates only from provenance: row key, route param, selected option, request query/body, or explicitly bound value.
- Reject ambiguous substring candidates.
- Template URL effects and standalone navigates using route provenance.

Runner changes:
- Render templates through typed codecs.
- Fail if required template params are missing.

Migration:
- Existing substring templates remain accepted but marked `template_source = legacy_substring`.
- Migration can replace unsafe templates when provenance is available.

Acceptance check:
- `btn-open-A-9001` templates to `btn-open-{asset_id}`, not `btn-open-{search}01`.
- `/asset/A-9001` becomes `/asset/{asset_id}` when derived from selected row key.

---

## WI-12. Build an annotator semantic clustering pipeline

Source: BLOCKER 3, STRATEGIC items 1 and 3, 17-matrix rows 1, 5, 6.

Why this ordering: Once schema and provenance exist, annotate must become the layer that collapses raw events into semantic units. The rejected alternative is making grabber debounce and decide semantic units; grabber lacks enough context and can only guess. This prevents replaying operator implementation details instead of intent.

Files to touch:
- `pilot/annotate.py:70-266`
- `pilot/skill_models.py:25-320`
- `pilot/agent/annotate_llm.py`
- `tests/agent/`

Skill schema changes:
- Add `SemanticCluster`: raw event IDs, cluster kind, primary target, effects, confidence, alternatives considered.
- Add `raw_event_ids` on `SkillStep`.

Grabber changes:
- Provide enough raw events; do not collapse semantic units in grabber except safe transport flushing.

Annotator changes:
- Replace linear event-to-step mapping with passes:
  1. normalize raw events;
  2. build causality/effect graph;
  3. detect widget clusters;
  4. bind params/provenance;
  5. produce semantic steps;
  6. add expected signals/assertions.

Runner changes:
- Execute semantic steps directly without re-deriving intent.

Migration:
- Legacy annotate mode can still output old linear steps behind a compatibility flag.

Acceptance check:
- A trace containing multiple typed input changes, Enter, and submit produces one `fill_submit` step.
- A trace containing multi-select open/search/check/close produces one `set_selection` step.

---

## WI-13. Preserve clear-field actions as real value transitions

Source: MAJOR row “Empty input after prior same target is dropped”.

Why this ordering: This is a focused annotator correction that depends on the clustering pipeline so clears are represented as final intended state, not noise. The rejected alternative is keeping the old empty-drop heuristic and hoping save assertions catch it. This prevents “clear field and save” workflows from silently preserving old values.

Files to touch:
- `pilot/annotate.py:85-89`
- `pilot/skill_models.py:90-108`
- `pilot/skill_runner.py:360-406`
- `tests/agent/`

Skill schema changes:
- Add `value_transition`: `from_recorded`, `to_recorded`, `clear_intent`.
- Allow empty string as valid required param/example.

Grabber changes:
- Capture input value before/after where possible.

Annotator changes:
- Remove empty input drop.
- Mark clears as semantic when followed by blur, submit, save, or field validation.

Runner changes:
- Fill empty string intentionally and verify field value is empty.

Migration:
- Existing recordings that already lost clear events cannot be recovered unless raw trace exists.

Acceptance check:
- Trace `title="Old"` → clear → save produces a `change`/`fill` with value `""` and replays to empty field.

---

## WI-14. Replace duplicate-click time dedupe with click gesture/effect classification

Source: MAJOR row “Duplicate click `<200ms` dropped”, 17-matrix rows 40, 41.

Why this ordering: This depends on causality/effect capture. The rejected alternative is adjusting the threshold; double-clicks, toggles, and repeated accordion clicks are semantic in some portals. This prevents dropping intentional double-click open, edit-cell activation, or two toggles that return a panel to its original state.

Files to touch:
- `pilot/annotate.py:93-101`
- `pilot/overlay/grabber.js:355-378`
- `pilot/skill_models.py`
- `pilot/skill_runner.py:339-359`

Skill schema changes:
- Add `click_gesture`: `single`, `double`, `repeat`, `toggle`, `open`, `close`.
- Add `effect_signature`.

Grabber changes:
- Record click detail/count, pointer type, target state before/after, and DOM effect evidence.

Annotator changes:
- Drop only provably duplicate transport noise.
- Preserve repeated clicks when effect or gesture semantics differ.

Runner changes:
- Execute double-click with Playwright double-click when annotated.
- For toggles, prefer desired state over repeated click count.

Migration:
- Legacy dropped clicks are unrecoverable without raw trace.
- Existing duplicated clicks remain valid.

Acceptance check:
- Double-click trace produces a double-click action.
- Two accordion toggles are preserved or converted into final desired state.

---

# Form Controls / Widget Semantics

## WI-15. Implement `fill_submit` for text + Enter submit

Source: 17-matrix row 30, BLOCKER 3.

Why this ordering: This is the first semantic cluster built on WI-12 and proves input coalescing. The rejected alternative is replaying multiple fills plus Enter plus submit; replay start state differs from recording state and duplicate submits can fire. This prevents typing repeated partial values or submitting twice.

Files to touch:
- `pilot/overlay/grabber.js:391-424`, `511-522`, `656-676`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py:360-469`
- `sample_portal/src/pages/Catalog.jsx:29-59`

Skill schema changes:
- Add `FillSubmitSpec`: field fingerprint, final value param, submit trigger (`enter`, `button`, `form_submit`), expected result signal.

Grabber changes:
- Capture Enter and submit causality to active input/form.
- Preserve raw input transitions.

Annotator changes:
- Collapse input burst + Enter/form submit into `fill_submit`.
- Deduplicate implicit submit caused by button click.

Runner changes:
- Fill final value once.
- Trigger the declared submit mechanism once.
- Wait for declared network/result DOM signal.

Migration:
- Legacy multiple change/key/submit sequences can be upgraded if adjacent and same form.

Acceptance check:
- Catalog search for `A-9003` emits one `fill_submit` and one result wait, not multiple fill steps.

---

## WI-16. Implement autocomplete/search-result selection semantics

Source: 17-matrix row 31.

Why this ordering: This depends on post-action waits and provenance templates so the selected result identity is not confused with query text. The rejected alternative is fill then click whatever visible result matches; network timing and result ordering vary. This prevents selecting stale or wrong autocomplete rows.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/src/pages/Catalog.jsx:29-145`
- `tests/agent/`

Skill schema changes:
- Add `AutocompleteSpec`: query param, result container fingerprint, option identity template, selected item param/provenance, network expectation.

Grabber changes:
- Capture listbox/result container snapshots after query.
- Attach result click to prior query and request.

Annotator changes:
- Collapse query + wait + result click into `select_autocomplete`.
- Separate `query` from `selected_item`.

Runner changes:
- Fill query, wait for result source, locate result by identity, fail on ambiguity or absence.

Migration:
- Legacy fill+click sequences can upgrade when click target is inside a result container rendered after the query request.

Acceptance check:
- Searching `A-90` and selecting `A-9003` replays with a different query/result set and still selects the intended item key.

---

## WI-17. Implement native single-select as typed enum selection

Source: 17-matrix row 32, MAJOR row “Select fuzzy threshold `0.7`”.

Why this ordering: Native select is simpler than cascading select and establishes enum behavior before dependencies. The rejected alternative is fuzzy-picking a closest option; “US”, “UAE”, and “UK” can be dangerously close. This prevents wrong status/locale/market choices.

Files to touch:
- `pilot/overlay/grabber.js:470-503`
- `pilot/annotate.py:164-181`
- `pilot/skill_models.py`
- `pilot/skill_runner.py:989-1052`
- `sample_portal/src/pages/Catalog.jsx:69-85`

Skill schema changes:
- Add `SelectOptionSpec`: recorded value, label, option snapshot, match mode (`value`, `label`, `alias`, `current_options`).
- Add alias map support.

Grabber changes:
- Capture full option list and selected option label/value.

Annotator changes:
- Emit `select_option` with enum param and option snapshot.

Runner changes:
- Select exact value first, then exact label or declared alias.
- Remove automatic fuzzy selection unless skill declares safe aliases.
- Fail with available options.

Migration:
- Existing `change` on `select` can be upgraded to `select_option`.
- Legacy fuzzy fallback can remain behind a compatibility policy but disabled for new skills.

Acceptance check:
- If recorded option is absent and no alias exists, runner fails structurally and lists available options.

---

## WI-18. Implement cascading select dependency model

Source: 17-matrix row 33, BLOCKER 2, sample Region → Market → Language design.

Why this ordering: This builds on native select and action-scoped waits. The rejected alternative is selecting recorded child values regardless of parent; those values may be invalid under new parent input. This prevents market/language selection from racing stale options or choosing impossible values.

Files to touch:
- `pilot/annotate.py`
- `pilot/skill_models.py:143-190`, `322-352`
- `pilot/skill_runner.py:360-406`, `1588-1698`
- `sample_portal/src/pages/AssetDetail.jsx:85-100`, `218-285`
- `sample_portal/mock_backend.js:260-267`

Skill schema changes:
- Use `SkillParam.depends_on`.
- Add `dependency_chain`, `option_source_request`, `option_signature_after`.
- Add `dependent_option_unavailable` error details.

Grabber changes:
- Capture request caused by parent select and child option signature after update.

Annotator changes:
- Detect select A changes followed by request and select B option mutation.
- Declare dependency and expected signal on parent select step.

Runner changes:
- Select parent, wait for child options, then select child from current options.
- Fail if requested dependent option unavailable; no fuzzy fallback.

Migration:
- Existing skills can upgrade dependencies when test IDs/names and request patterns indicate cascades.

Acceptance check:
- Replaying with a different region waits for that region’s markets and fails cleanly if recorded market is not valid for the new region.

---

## WI-19. Auto-collapse custom multi-select into `set_selection`

Source: BLOCKER 3, 17-matrix row 34.

Why this ordering: This depends on clustering, typed list params, and option identity templates. The rejected alternative is replaying open/search/check sequences; that cannot handle different set sizes. This prevents brittle category/tag workflows.

Files to touch:
- `pilot/annotate.py`
- `pilot/skill_models.py:217-270`
- `pilot/skill_runner.py:502-699`
- `sample_portal/src/components/MultiSelect.jsx:57-130`
- `sample_portal/src/pages/AssetDetail.jsx:194-205`

Skill schema changes:
- Extend `SetSelectionSpec` with option source, selected item label/value mapping, search behavior, final equality assertion, close behavior.

Grabber changes:
- Capture popover open, search input, checkbox/chip IDs, selected chips before/after.

Annotator changes:
- Detect common test ID prefixes and ARIA listbox/checkbox patterns.
- Collapse to one `set_selection` with list param.

Runner changes:
- Reconcile current set to target set.
- Verify final chips equal target.
- Treat removal/commit failures as structured failures, not silent success.

Migration:
- Existing hand-authored `set_selection` remains valid.
- Linear multiselect traces can upgrade when prefix pattern is detected.

Acceptance check:
- Recording categories `[sports, drama]` replays with `[kids]` and final chips equal exactly `[kids]`.

---

## WI-20. Support nested/searchable/dependent multi-selects

Source: 17-matrix row 35.

Why this ordering: This extends WI-19 with dependencies and network-backed option sources. The rejected alternative is treating nested pickers as flat set-selection; parent filters may hide valid children. This prevents selecting tags/categories from the wrong parent or stale filtered option list.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py:502-699`
- `sample_portal/` test fixture additions

Skill schema changes:
- Add `SetSelectionSpec.depends_on`, `parent_picker_fp`, `option_source`, `search_result_signal`, `hierarchy_path`.

Grabber changes:
- Capture parent selection, search/filter input, and option refresh requests.

Annotator changes:
- Group parent dropdown + child multiselect as one dependent set operation when appropriate.

Runner changes:
- Set parent context first, wait for child options, then reconcile child set.

Migration:
- Existing flat `set_selection` unaffected.

Acceptance check:
- Nested picker replay with a different parent selects only options valid under that parent.

---

## WI-21. Implement native and custom date picker semantics

Source: 17-matrix row 36, What You Missed item 57.

Why this ordering: Dates require typed codecs and locale/timezone policy. The rejected alternative is replaying calendar cell clicks; a different target date may need a different navigation path. This prevents selecting the wrong date due to calendar month offsets or locale formatting.

Files to touch:
- `pilot/overlay/grabber.js:498-503`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/src/pages/AssetDetail.jsx:317-320`
- `tests/agent/`

Skill schema changes:
- Add `DatePickerSpec`: native/custom, value param, display format, timezone, calendar controls, selected day identity.

Grabber changes:
- Capture native date values.
- For custom date pickers, capture calendar role/grid/cell metadata and control navigation.

Annotator changes:
- Native: emit date param directly.
- Custom: detect date picker cluster and produce `date_select`.

Runner changes:
- Native: set ISO-compatible value and dispatch events.
- Custom: navigate widget by semantic date, not recorded click sequence.

Migration:
- Legacy native date `change` upgrades safely.
- Legacy custom date clicks require raw trace or remain generic with warning.

Acceptance check:
- Recording one publish date replays a different date without requiring the same calendar cell click path.

---

# Locator Resolution / Repair Safety

## WI-22. Run ambiguity detection at every locator level

Source: MAJOR row “Ambiguity only fires for templated `test_id` / `element_id`”.

Why this ordering: This should land before L3 repair and table work because ambiguity is the safety boundary for “many Open buttons.” The rejected alternative is detecting only templated IDs; L2 role/name matches are the common ambiguous case. This prevents `.first` from mutating the wrong row/card.

Files to touch:
- `pilot/skill_runner.py:754-837`, `913-954`, `1133-1182`
- `pilot/skill_models.py:193-215`
- `pilot/agent/orchestrator.py:801-925`
- `curationpilot-app/src/routes/ReplayPage.tsx:771-862`

Skill schema changes:
- Add `locator_scope`, `expected_candidate_count`, `ambiguity_policy`, `candidate_context_fields`.

Grabber changes:
- Capture row/card/section context in fingerprints.

Annotator changes:
- Mark row-scoped actions with expected unique context.

Runner changes:
- Count visible candidates for L1, L2, alternates, and repaired locators.
- Surface `ambiguous_target` with candidate context before acting.

Migration:
- Legacy steps default to `ambiguity_policy = fail_if_multiple` for destructive/click actions.

Acceptance check:
- Multiple visible “Open” buttons produce a row-picker pause instead of clicking the first.

---

## WI-23. Replace `_first_visible` exception masking with locator diagnostics

Source: MAJOR row “`_first_visible` catches all exceptions then uses `count() > 0`”.

Why this ordering: Ambiguity and repair depend on trustworthy locator candidate checks. The rejected alternative is narrower exception handling with the same boolean result; we need structured diagnostics. This prevents hidden or strict-mode-invalid elements from being treated as usable.

Files to touch:
- `pilot/skill_runner.py:1133-1182`, `1948-1957`
- `pilot/models.py`
- `tests/agent/`

Skill schema changes:
- None beyond diagnostic support.

Grabber changes:
- None.

Annotator changes:
- None.

Runner changes:
- Replace `_first_visible` with `LocatorProbeResult`.
- Distinguish zero matches, multiple matches, hidden, detached, strict-mode error, timeout.
- Never convert visibility failure into success via `count() > 0`.

Migration:
- Non-breaking internal change.

Acceptance check:
- Hidden matching element is rejected with `locator_not_visible`, not clicked.

---

## WI-24. Use safe selector construction instead of partial CSS escaping

Source: MAJOR row “CSS escape only handles `\` and `'`”.

Why this ordering: Locator correctness underpins all later widgets, especially names/IDs with enterprise-generated characters. The rejected alternative is expanding the Python escape helper manually; browser `CSS.escape` and Playwright semantic locators are safer. This prevents selectors breaking on `]`, quotes, spaces, colons, and non-ASCII IDs.

Files to touch:
- `pilot/skill_runner.py:1137-1142`, `1944-1945`
- `pilot/overlay/grabber.js:146-151`
- `pilot/skill_models.py:_build_replay_selectors`

Skill schema changes:
- None.

Grabber changes:
- Keep using browser `CSS.escape` where available.

Annotator changes:
- Avoid generating raw attribute selectors when semantic locators exist.

Runner changes:
- Use Playwright locator APIs or evaluate `CSS.escape` in page context.
- Escape `[name=...]` and replay selector exports correctly.

Migration:
- Non-breaking.

Acceptance check:
- IDs/names containing `]`, quotes, spaces, and colons resolve correctly.

---

## WI-25. Replace automatic fuzzy select with declared aliases and structural failure

Source: MAJOR row “Select fuzzy threshold `0.7`”.

Why this ordering: This finalizes native/cascading select safety. The rejected alternative is changing the threshold; any threshold can silently select the wrong locale/status. This prevents wrong enum selection in enterprise workflows.

Files to touch:
- `pilot/skill_runner.py:989-1052`
- `pilot/skill_models.py`
- `pilot/annotate.py`
- `tests/agent/`

Skill schema changes:
- Add `enum_aliases`, `allow_fuzzy = false` default, `option_match_policy`.

Grabber changes:
- Capture option labels/values for alias suggestion.

Annotator changes:
- Populate exact value/label.
- Suggest aliases only from observed option metadata or portal config.

Runner changes:
- Exact value, exact label, declared alias.
- Otherwise fail `option_not_available`.

Migration:
- Existing skills using implicit fuzzy can opt into legacy policy, but new skills must not.

Acceptance check:
- Recorded `US` never auto-selects `UAE`; runner fails unless alias explicitly declares it.

---

## WI-26. Rework L3 locator repair around uniqueness and evidence, not fixed score bands

Source: MAJOR row “L3 repair thresholds `0.85/0.65/0.55`”.

Why this ordering: This depends on locator diagnostics and ambiguity. The rejected alternative is making thresholds portal-configurable; scores are not meaningful without uniqueness and postconditions. This prevents self-heal from selecting the wrong repeated “Approve” button.

Files to touch:
- `pilot/agent/locator_repair.py:185-191`
- `pilot/skill_runner.py:1190-1240`, `1785-1833`
- `tests/agent/test_locator_repair.py`

Skill schema changes:
- Add `RepairPolicy`: required matching features, uniqueness scope, allowed drift fields, required postcondition.

Grabber changes:
- Capture richer candidate context.

Annotator changes:
- Emit repair policy from original fingerprint quality and action risk.

Runner changes:
- Accept repair only if unique under policy and verified by action-specific assertion.
- Treat “medium” repair as pause unless declared safe.

Migration:
- Existing alternates remain usable but are verified under new policy.

Acceptance check:
- Two similar “Approve” buttons force ambiguity/human choice even if one has high text similarity.

---

## WI-27. Replace L3 verification page signature with action-specific postconditions

Source: MAJOR row “L3 verification uses URL/text length/interactable count”, STRATEGIC item 3.

Why this ordering: This depends on expected signals/assertions and must land before persisting healed locators. The rejected alternative is improving the signature; whole-page length can change for unrelated reasons. This prevents persisting a wrong locator because a spinner changed text.

Files to touch:
- `pilot/skill_runner.py:1785-1833`
- `pilot/skill_models.py:108-142`
- `pilot/annotate.py`
- `pilot/agent/executor_real.py:520-641`

Skill schema changes:
- Expand `StepAssertion` with `url_matches_template`, `field_value_equals`, `selection_equals`, `toast_visible`, `request_completed`, `download_started`.

Grabber changes:
- Capture post-action observed DOM/network diff for assertion suggestions.

Annotator changes:
- Add assertions for navigation, save, selection, modal, download, validation, and toast outcomes.

Runner changes:
- Verify declared assertions after action.
- Persist heals only when assertions pass.

Migration:
- Legacy skills without assertions fall back to conservative repair: do not persist medium-risk heals automatically.

Acceptance check:
- L3 click that changes unrelated text but not the expected field/status fails verification.

---

# Additional Interaction Types

## WI-28. Capture and replay sliders/range controls

Source: 17-matrix row 37.

Why this ordering: Sliders depend on typed numeric params and control metadata. The rejected alternative is waiting for `change` only; many sliders emit meaningful `input` events during drag. This prevents missing slider interactions entirely.

Files to touch:
- `pilot/overlay/grabber.js:427-454`, `470-503`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/` slider fixture

Skill schema changes:
- Add `SliderSpec`: min, max, step, orientation, value param, event mode.

Grabber changes:
- Capture `input[type=range]` input/change with final value and metadata.

Annotator changes:
- Collapse drag burst to one `slider_set`.

Runner changes:
- Set value through DOM/Playwright and dispatch expected input/change events.
- Verify final value.

Migration:
- Legacy traces likely lack range events; no automatic recovery.

Acceptance check:
- Moving slider during teach creates one semantic `slider_set`; replay sets a different value and verifies it.

---

## WI-29. Strengthen file upload modeling

Source: 17-matrix row 38.

Why this ordering: File upload already exists but records filename only, so it needs typed param and validation work. The rejected alternative is using recorded filename as path; replay needs a new local file path. This prevents late Playwright failures or uploading the wrong file shape.

Files to touch:
- `pilot/overlay/grabber.js:482-490`
- `pilot/annotate.py:164-181`
- `pilot/skill_runner.py:407-438`
- `pilot/agent/executor_real.py:300-350`
- `tests/fixtures/`

Skill schema changes:
- Add `FileSpec`: original name, extension, MIME if available, size if available, accept attribute, multiple flag.
- Param type remains `file_path` or `file_path_list`.

Grabber changes:
- Capture file metadata, not path.

Annotator changes:
- Bind replay param to file path and preserve shape constraints.

Runner changes:
- Validate path exists, matches constraints, and upload completed via assertion.

Migration:
- Existing `file_selected` steps map to `FileSpec` with partial metadata.

Acceptance check:
- Replaying with a different file path succeeds when constraints match and fails before page mutation when file missing.

---

## WI-30. Add drag-and-drop recording and replay

Source: 17-matrix row 39.

Why this ordering: Drag/drop needs action schema and pointer event capture. The rejected alternative is synthesizing clicks on source/target; many portals require `DataTransfer` semantics. This prevents unrecorded drag workflows from disappearing.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/skill_models.py`
- `pilot/annotate.py`
- `pilot/skill_runner.py`
- `sample_portal/` drag/drop fixture

Skill schema changes:
- Add `DragDropSpec`: source fingerprint, target fingerprint, data payload summary, drop effect, coordinates policy.

Grabber changes:
- Capture `dragstart`, `dragover`, `drop`, pointer source/target, and dataTransfer types.

Annotator changes:
- Collapse drag event sequence into `drag_drop`.

Runner changes:
- Use Playwright drag APIs where possible; fall back to DataTransfer JS only when declared safe.
- Verify target state.

Migration:
- No automatic migration for absent drag events.

Acceptance check:
- Dragging an item into a drop zone records/replays and asserts the item appears in the target.

---

## WI-31. Make LLM annotate enrich structure, not only names

Source: STRATEGIC item 5.

Why this ordering: Deterministic structural annotate should land first; LLM should improve uncertain structure, not invent it. The rejected alternative is asking LLM to infer everything from a flat step list; that creates hallucinated dependencies. This prevents LLM output from being cosmetic while structural fields remain empty.

Files to touch:
- `pilot/agent/annotate_llm.py:1-260`
- `pilot/agent/schemas/skill.py`
- `pilot/annotate.py`
- `curationpilot-app/src/protocol/types.ts`

Skill schema changes:
- Extend v2 sidecar with structural overlays: `depends_on`, `expected_signals`, `assert_after`, `widget_type`, `risk_notes`, `param_alias_map`.

Grabber changes:
- None.

Annotator changes:
- Provide deterministic structural candidates to LLM for labeling/confirmation.
- Validate LLM structural suggestions against raw trace evidence.

Runner changes:
- Consume structural overlays after validation.

Migration:
- Existing v2 sidecars remain valid; new fields optional.

Acceptance check:
- LLM pass can rename params and confirm a detected cascade/multiselect, but cannot add a dependency with no supporting trace/request evidence.

---

## WI-32. Implement iframe and shadow DOM traversal

Source: MINOR 24, What You Missed item 48.

Why this ordering: Schema fields already exist but are unused, so this unlocks embedded enterprise components before advanced widgets are reliable. The rejected alternative is leaving fields as documentation-only; that creates false confidence. This prevents failures in embedded DAM/SSO/preview widgets and shadow-root design systems.

Files to touch:
- `pilot/overlay/grabber.js:299-321`
- `pilot/skill_models.py:69-71`
- `pilot/skill_runner.py:1133-1182`
- `pilot/agent/locator_repair.py`
- `tests/agent/`

Skill schema changes:
- Define `frame_path` and `shadow_path` semantics precisely.
- Replace boolean-only `in_shadow_root` with path metadata.

Grabber changes:
- Inject into same-origin iframes where possible.
- Capture frame selector/path and shadow host chain.

Annotator changes:
- Preserve frame/shadow context in fingerprints and clusters.

Runner changes:
- Traverse frames before locator resolution.
- Resolve shadow roots through Playwright locators/evaluate handles.

Migration:
- Existing `frame_path: []` remains top document.
- Existing `in_shadow_root: true` without path becomes non-replayable warning.

Acceptance check:
- Element inside same-origin iframe and element inside shadow root can be recorded and replayed.

---

## WI-33. Model accordion/collapse panels as desired state

Source: 17-matrix row 40, MAJOR row “Duplicate click `<200ms` dropped”.

Why this ordering: This depends on click effect classification and readiness assertions. The rejected alternative is replaying header clicks blindly; current state may differ. This prevents collapsing an already-expanded panel.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/` accordion fixture

Skill schema changes:
- Add `ToggleStateSpec`: target state, state attribute (`aria-expanded`), controlled panel selector.

Grabber changes:
- Capture `aria-expanded` before/after and controlled panel visibility.

Annotator changes:
- Convert click on accordion header into `toggle_state`.

Runner changes:
- Check current state first.
- Click only if state differs.
- Verify target state.

Migration:
- Legacy accordion clicks remain generic unless state metadata exists.

Acceptance check:
- Replay leaves panel expanded whether it starts collapsed or already expanded.

---

## WI-34. Model modal open/close effects

Source: 17-matrix row 41.

Why this ordering: Modal semantics depend on effect assertions and global key handling. The rejected alternative is replaying backdrop/root clicks or Escape on whichever input was focused. This prevents fragile modal close/open workflows.

Files to touch:
- `pilot/overlay/grabber.js:355-378`, `656-676`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `curationpilot-app/src/routes/ReplayPage.tsx` if UI fixture needed

Skill schema changes:
- Add `ModalSpec`: open/close, dialog fingerprint, close mechanism, expected visibility.

Grabber changes:
- Capture dialog appearance/disappearance and Escape/backdrop/close button causality.
- Capture global Escape, not only input Escape.

Annotator changes:
- Collapse open click + dialog appears into modal open.
- Collapse close button/backdrop/Escape + dialog hidden into modal close.

Runner changes:
- Open/close using declared mechanism.
- Assert dialog visibility/hidden state.

Migration:
- Legacy modal root clicks can upgrade only with raw DOM diff evidence.

Acceptance check:
- Modal close via Escape replays as global Escape and verifies dialog hidden.

---

## WI-35. Capture popup/new-window workflows

Source: 17-matrix row 43.

Why this ordering: Popup handling depends on causality and cross-page runner context. The rejected alternative is letting Playwright remain on the original page; later locators run in the wrong tab. This prevents losing workflows that open upload/preview/auth windows.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/skill_models.py`
- `pilot/annotate.py`
- `pilot/skill_runner.py`
- `pilot/browser.py`
- `pilot/agent/executor_real.py`

Skill schema changes:
- Add `PopupEffect`: target URL template, window name, page binding key, switch policy, close policy.

Grabber changes:
- Hook `window.open` and record page creation when available.
- Capture target URL and opener interaction.

Annotator changes:
- Fold popup into causing click effect.
- Emit page context switch steps when workflow continues in popup.

Runner changes:
- Use Playwright `expect_popup`.
- Bind subsequent steps to the new page until switch/close step.

Migration:
- Existing skills cannot infer popups unless trace has navigation/page evidence.

Acceptance check:
- Clicking a button that opens a popup causes runner to switch to popup and execute next step there.

---

## WI-36. Separate auth preconditions from business workflow recording

Source: 17-matrix row 44.

Why this ordering: Auth affects preflight and teach safety. The rejected alternative is allowing login/password steps inside every business skill; that leaks secrets and makes business workflows brittle. This prevents replay from filling recorded credentials or failing midway due to session expiry.

Files to touch:
- `pilot/agent/schemas/portal_context.py:48-73`
- `pilot/agent/executor_real.py:119-223`
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `sample_portal/src/pages/Login.jsx`

Skill schema changes:
- Add `preconditions.auth_required`, `auth_scope`, `redacted_param`, `secret_param`.
- Add `bootstrap_skill` reference for optional login automation.

Grabber changes:
- Mark password/OTP fields sensitive and redact values.
- Detect login/auth pages through portal context/auth signals.

Annotator changes:
- Split auth steps from business skill or mark as bootstrap.
- Never create plaintext password params.

Runner changes:
- Preflight auth before skill.
- Pause for operator auth if missing.
- Refuse to replay sensitive captured literal values.

Migration:
- Existing skills containing password fields are flagged unsafe and require re-annotation.

Acceptance check:
- Recording login does not store plaintext password; business replay pauses for auth instead of replaying credentials.

---

## WI-37. Model virtualized/paginated tables as collection queries

Source: 17-matrix row 45.

Why this ordering: This depends on search/autocomplete, scroll, ambiguity, and provenance. The rejected alternative is clicking row N or a recorded test ID directly; virtualized rows may not exist until scrolled or paged. This prevents selecting wrong or absent table rows in large catalogs.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/` virtual table fixture

Skill schema changes:
- Add `CollectionQuerySpec`: row key param, search/filter/sort/page controls, row locator template, load strategy.

Grabber changes:
- Capture table row keys, pagination controls, virtual scroll container, and search/filter requests.

Annotator changes:
- Recognize table row click as collection selection by row key.

Runner changes:
- Search/filter/page/scroll until row key visible.
- Fail `row_not_found` with available context.

Migration:
- Existing row test ID templates can upgrade when row key provenance exists.

Acceptance check:
- Replay selects row by asset ID even when it starts on another page or outside virtualized viewport.

---

## WI-38. Capture meaningful scroll as `scroll_until` / lazy-load intent

Source: 17-matrix row 46.

Why this ordering: Scroll is needed by virtualized tables and hover/mega-menu visibility. The rejected alternative is capturing all scroll noise; most scrolls are incidental. This prevents missing infinite-scroll hydration while avoiding noisy replay.

Files to touch:
- `pilot/overlay/grabber.js:7-9`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/` scroll fixture

Skill schema changes:
- Add `ScrollSpec`: container fingerprint, direction, target condition, load request pattern, item key.

Grabber changes:
- Capture scroll only when followed by DOM growth, network load, or target visibility change.
- Record scroll container identity.

Annotator changes:
- Convert meaningful scroll into `scroll_until`.

Runner changes:
- Scroll container until target assertion or load exhaustion.
- Fail structurally if target never appears.

Migration:
- Old recordings lack scroll; Playwright auto-scroll remains fallback for normal locators.

Acceptance check:
- Infinite-scroll list replay scrolls until target row key is visible, then clicks it.

---

# “What You Missed” Coverage

## WI-39. Support contenteditable and rich text editors

Source: What You Missed item 47.

Why this ordering: Depends on typed control metadata and keyboard handling. The rejected alternative is treating rich text as plain input; editors may require selection, formatting, or editor-specific events. This prevents replay from failing to set descriptions/comments in CMS editors.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/` rich text fixture

Skill schema changes:
- Add `RichTextSpec`: plain text, HTML snapshot, mode, formatting commands, editor root.

Grabber changes:
- Capture `beforeinput`, `input`, selection metadata, contenteditable root, and sanitized HTML/text.

Annotator changes:
- Collapse editor input bursts into `rich_text_set`.

Runner changes:
- Prefer editor API/DOM text insertion policy declared by spec.
- Verify text/HTML content.

Migration:
- Legacy clicks/keys in editors remain generic with warning.

Acceptance check:
- Recording a contenteditable note replays a different note and verifies editor content.

---

## WI-40. Support hover menus and mega-menus

Source: What You Missed item 49.

Why this ordering: Hover menus depend on pointer event capture and modal/dropdown-like visibility assertions. The rejected alternative is clicking hidden menu items directly; the menu may not be rendered until hover. This prevents failing to access navigation/actions hidden in enterprise menus.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/` hover menu fixture

Skill schema changes:
- Add `HoverRevealSpec`: hover target, revealed container, item target, persistence policy.

Grabber changes:
- Capture pointerenter/mouseover only when it causes a menu/container to appear.

Annotator changes:
- Collapse hover + click into `hover_reveal_click` or explicit hover effect.

Runner changes:
- Hover, wait for revealed menu, then click item.

Migration:
- Existing hidden menu clicks cannot reliably migrate without raw hover evidence.

Acceptance check:
- Mega-menu item replay succeeds only after hover reveals its container.

---

## WI-41. Support global keyboard shortcuts

Source: What You Missed item 50.

Why this ordering: Depends on separating focused input keys from global shortcuts. The rejected alternative is only capturing Enter/Escape on inputs; enterprise apps use Ctrl/Meta shortcuts. This prevents missing save/search/open commands triggered by keyboard.

Files to touch:
- `pilot/overlay/grabber.js:656-676`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`

Skill schema changes:
- Add `ShortcutSpec`: key combo, scope, expected effect, target focus policy.

Grabber changes:
- Capture keydown for meaningful modifier combos globally.
- Avoid capturing sensitive text/password input.

Annotator changes:
- Classify shortcut by effect and scope.

Runner changes:
- Ensure focus policy, press shortcut, verify effect.

Migration:
- Existing traces only have Enter/Escape; no auto-recovery.

Acceptance check:
- Ctrl+S save shortcut records/replays and verifies save signal.

---

## WI-42. Model toast-driven undo and conflict-resolution flows

Source: What You Missed item 51.

Why this ordering: Depends on toast assertions and fail-fast safety. The rejected alternative is ignoring transient toasts; undo/conflict choices may be the actual workflow branch. This prevents missing “Undo”, “Resolve conflict”, or “Retry save” decisions.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `curationpilot-app/src/routes/ReplayPage.tsx` if pause UI extends

Skill schema changes:
- Add `ToastSpec`: message matcher, action button fingerprint, expiry policy, conflict kind.

Grabber changes:
- Capture toast appearance, text, action buttons, and disappearance.

Annotator changes:
- Associate toast with prior mutation.
- Model undo/conflict action as semantic branch.

Runner changes:
- Wait for expected toast.
- Click declared toast action when part of workflow.
- Fail if conflict toast appears unexpectedly.

Migration:
- Existing skills without toast modeling continue but may miss transient branches.

Acceptance check:
- Save conflict toast pauses or follows declared resolution instead of silently continuing.

---

## WI-43. Implement disabled-to-enabled readiness as first-class wait

Source: What You Missed item 52, MAJOR row “Generic settle numbers”.

Why this ordering: This refines WI-10 for controls whose readiness is the core condition. The rejected alternative is relying on Playwright auto-wait; auto-wait checks actionability but not semantic readiness of dependent fields. This prevents uploading or selecting before a control is enabled by prior state.

Files to touch:
- `pilot/overlay/grabber.js:299-321`
- `pilot/annotate.py`
- `pilot/skill_models.py:161-190`
- `pilot/skill_runner.py:1588-1698`
- `scripts/operator_test_suite.py:130-150`

Skill schema changes:
- Add DOM expectation kind `enabled`, `disabled`, `value_ready`.

Grabber changes:
- Capture disabled attribute transitions after actions.

Annotator changes:
- Attach `enabled` wait to the action that enables the next control.

Runner changes:
- Wait for enabled state before interacting.
- Fail `control_not_enabled` when required readiness never happens.

Migration:
- Non-breaking; legacy relies on Playwright action timeout.

Acceptance check:
- File input enabled after content selection is awaited explicitly before upload.

---

## WI-44. Capture server validation errors bound to fields

Source: What You Missed item 53.

Why this ordering: Depends on action-scoped network and diagnostics. The rejected alternative is treating failed save as generic postcondition failure; operators need field-specific errors. This prevents continuing after validation rejected data.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `sample_portal/mock_backend.js`
- `sample_portal/src/pages/AssetDetail.jsx:142-144`

Skill schema changes:
- Add `ValidationErrorSpec`: field selector, message selector, server status, response field path.

Grabber changes:
- Capture validation message DOM changes and failed response body shape when available.

Annotator changes:
- Associate field errors with submitted step.

Runner changes:
- On validation error, fail with `server_validation_error` and field/message details.

Migration:
- Non-breaking.

Acceptance check:
- A rejected submit surfaces field-specific validation details and stops replay.

---

## WI-45. Support download/export workflows

Source: What You Missed item 54.

Why this ordering: Downloads are action effects like popups and must be modeled before reporting/export portals work. The rejected alternative is clicking export and assuming success; browser downloads require explicit handling. This prevents losing generated files or continuing before export completes.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/annotate.py`
- `pilot/skill_models.py`
- `pilot/skill_runner.py`
- `pilot/browser.py`
- `pilot/agent/web_server.py`

Skill schema changes:
- Add `DownloadEffect`: suggested filename template, MIME/type, save policy, path param.

Grabber changes:
- Capture click intent and observed download trigger where browser APIs allow.

Annotator changes:
- Fold export click into click with download effect.

Runner changes:
- Use Playwright download expectation.
- Save to session artifact or supplied path.
- Verify file exists and optionally validate type.

Migration:
- Existing export clicks remain generic unless re-recorded.

Acceptance check:
- Export click produces a downloaded file artifact path in replay result.

---

## WI-46. Support cross-tab workflows

Source: What You Missed item 55.

Why this ordering: Cross-tab extends popup handling but includes switching between existing pages. The rejected alternative is one active `session.page`; workflows involving admin/preview tabs will execute steps in the wrong page. This prevents cross-tab preview/approval sequences from failing or mutating wrong context.

Files to touch:
- `pilot/browser.py`
- `pilot/skill_runner.py`
- `pilot/skill_models.py`
- `pilot/agent/executor_real.py`
- `pilot/overlay/grabber.js`

Skill schema changes:
- Add `PageContextSpec`: page key, URL matcher, title matcher, opener relation, active step scope.

Grabber changes:
- Capture active page/tab identity and switches when possible through CDP side.

Annotator changes:
- Assign each step to a page context.

Runner changes:
- Maintain page registry and switch by context before each step.

Migration:
- Legacy skills default to single page context.

Acceptance check:
- Workflow opens preview tab, verifies preview, returns to original tab, and continues.

---

## WI-47. Model WebSocket/SSE-driven state changes

Source: What You Missed item 56, MAJOR row “Generic settle numbers”.

Why this ordering: This builds on wait engine and portal network ignore. The rejected alternative is ignoring long-lived channels; state may update only through push events. This prevents waiting forever on SSE or missing async workflow completion.

Files to touch:
- `pilot/overlay/grabber.js:36-113`
- `pilot/skill_runner.py:1285-1698`
- `pilot/agent/schemas/portal_context.py:104-113`
- `pilot/skill_models.py`

Skill schema changes:
- Add `PushSignalExpectation`: channel type, message predicate, DOM projection selector, ignore policy.

Grabber changes:
- Hook `WebSocket` and `EventSource` metadata where safe.
- Capture message summaries without sensitive payload logging by default.

Annotator changes:
- Associate push-driven DOM changes with prior action.

Runner changes:
- Ignore long-lived channels for generic idle.
- Wait for declared push message or resulting DOM assertion.

Migration:
- Existing `network_ignore` remains compatible.

Acceptance check:
- A workflow state update delivered through SSE satisfies a declared signal; generic idle does not wait forever.

---

## WI-48. Add locale/timezone-aware date and number handling

Source: What You Missed item 57, 17-matrix row 36.

Why this ordering: This extends typed params and date picker work. The rejected alternative is storing formatted strings only; replay on a different locale/timezone can parse/display wrong values. This prevents off-by-one dates and decimal/thousands parsing mistakes.

Files to touch:
- `pilot/agent/schemas/portal_context.py:31-46`
- `pilot/skill_models.py`
- `pilot/annotate.py`
- `pilot/skill_runner.py`

Skill schema changes:
- Add `LocalePolicy`: locale, timezone, date format, number format, calendar system.
- Add param codec fields for localized date/number.

Grabber changes:
- Capture browser locale/timezone and input/display values.

Annotator changes:
- Normalize param values to canonical representation while preserving display format.

Runner changes:
- Format canonical params for target control according to portal policy.

Migration:
- Legacy date/number strings remain raw unless portal context declares conversion.

Acceptance check:
- A date recorded in one display format replays correctly under declared portal timezone/locale.

---

## WI-49. Support canvas/SVG/media timeline controls through adapters

Source: What You Missed item 58.

Why this ordering: Canvas/media controls cannot be solved by generic DOM locators, so they should come after core DOM interactions. The rejected alternative is storing raw coordinates only; layout changes break coordinate replay. This prevents media scrubbers, SVG charts, and canvas editors from being unrecordable.

Files to touch:
- `pilot/overlay/grabber.js`
- `pilot/skill_models.py`
- `pilot/annotate.py`
- `pilot/skill_runner.py`
- `pilot/adapters/`

Skill schema changes:
- Add `NonDomControlSpec`: adapter name, semantic value, coordinate fallback, bounding box, calibration anchors.

Grabber changes:
- Capture pointer events on canvas/SVG/media controls with bounding boxes and ARIA/value metadata if available.

Annotator changes:
- Prefer known adapters for media timeline, SVG slider, canvas hotspots.
- Store coordinate fallback only with calibration anchors.

Runner changes:
- Execute through adapter; verify semantic value or visual state where possible.

Migration:
- Existing coordinate-only clicks remain generic and flagged fragile.

Acceptance check:
- Media timeline scrub records target position semantically and replays after element resize.

---

# Final Integration / Verification

## WI-50. Build the full regression matrix for all fixed findings

Source: All BLOCKERS, all MAJOR rows, all MINOR items, all STRATEGIC items, all 17-matrix rows, all What You Missed items.

Why this ordering: This comes after the implementation items because it verifies the end-to-end behavior rather than a single helper. The rejected alternative is isolated unit tests only; the failures are mostly cross-layer teach → annotate → replay bugs. This prevents regressions where grabber records the right thing but annotator or runner loses it.

Files to touch:
- `tests/agent/`
- `scripts/e2e_test.py`
- `scripts/resilience_test.py`
- `scripts/operator_test_suite.py`
- `sample_portal/src/`
- `sample_portal/mock_backend.js`

Skill schema changes:
- None beyond prior work.

Grabber changes:
- Add fixture coverage for every captured event family.

Annotator changes:
- Add golden-trace tests for every semantic cluster.

Runner changes:
- Add replay tests for every action type and failure mode.

Migration:
- Include legacy skill load/replay tests to prove backward compatibility.

Acceptance check:
- Automated matrix covers:
  - click-caused navigation without duplicate `goto`;
  - post-action expected signals;
  - text submit;
  - autocomplete;
  - native/cascading selects;
  - multiselect/nested multiselect;
  - date, slider, upload, drag/drop;
  - accordion, modal, popup, auth;
  - virtualized table, scroll;
  - contenteditable, iframe/shadow, hover, shortcuts;
  - toast conflict, disabled readiness, server validation;
  - download, cross-tab, WebSocket/SSE, locale/timezone, canvas/media;
  - all hardcoded heuristic replacements.
