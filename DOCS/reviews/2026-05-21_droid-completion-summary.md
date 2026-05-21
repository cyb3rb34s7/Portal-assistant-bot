# 2026-05-21 - Structural fix sprint completion

Canonical completion log for the CurationPilot teach / annotate / replay
structural fix. Covers the foundation drift fixes (F-01..F-10) plus
work items WI-01..WI-50 as described in
[`2026-05-21_fix-everything-plan.md`](2026-05-21_fix-everything-plan.md).

Branch: `feat/enterprise-portal-and-additive-sprint`. All commits below
are reachable from the branch head at write time.

## Phase 0 - foundation drifts (F-01..F-10)

Source audit: [`2026-05-21_foundation-verification.md`](2026-05-21_foundation-verification.md).

| F-N | Description | Commit | Test file |
|---|---|---|---|
| F-01 | Add typed `OptionSnapshot` model so the grabber's bool `selected=true` no longer downgrades select fingerprints to None | `2ba2503` | `tests/agent/test_skill_models.py` |
| F-02 | Sample portal retry safety: `portals/sample_portal/context.yaml` sets `allow_existing_header: false` so the runner's stable key wins | `0aebae6` | `tests/agent/test_idempotency.py` |
| F-03 | XHR shim reads `cfg.header_name` instead of the hardcoded `idempotency-key` literal | `7c3b877` | `tests/agent/test_idempotency.py` |
| F-04 | Fetch wrapper detects `input instanceof Request` and seeds augmentation from `Request.headers` | `3aa5f3a` | `tests/agent/test_idempotency.py` |
| F-05 | `run_annotate` builds the causality graph BEFORE `filter_events`, so the graph sees the raw event stream | `ed0eefb` | `tests/agent/test_trace_causality.py` |
| F-06 | Grabber populates `TraceEvent.page_state_before / page_state_after` with `{url, title, key_dom_signature}` | `b7a8595` | `tests/agent/test_skill_models.py` |
| F-07 | Emit `network_request` / `network_response` / `dom_mutation` as `TraceEvent`s with request_id + initiator_event_id | `9fe1c18` | `tests/agent/test_skill_models.py` |
| F-08 | Remove hardcoded heuristics introduced in WI-01..04 (3000ms attribution window, opts cap, wait defaults, idempotency fail-open) | `82ed6e2` | `tests/agent/test_idempotency.py`, `tests/agent/test_action_baselines_wi09.py` |
| F-09 | `ReplayPolicy` normalizes `optional=True` + `abort` to `optional`; `ParamProvenance.confidence` constrained 0..1; `TraceEvent.kind` Literal expanded; `NetworkExpectation.started_after_event` added | `a78c04c` | `tests/agent/test_skill_models.py` |
| F-10 | Pre-existing test stub `OneTimeFailExecutor.execute` accepts `sub_step_overrides` kwarg the orchestrator passes on retry | `4bfe28c` | `tests/agent/test_integration_multi_item.py` |

All ten foundation drifts are CLOSED. Each had a dedicated commit and at
least one regression test.

## Phase 1 - WI-01..WI-50

Plan: [`2026-05-21_fix-everything-plan.md`](2026-05-21_fix-everything-plan.md).
The first batch (WI-01..WI-04, WI-07) was committed before this sprint's
completion documents; the F-N drifts above corrected drifts found in
that batch.

| WI | Description | Commit | Primary test file | Acceptance check |
|---|---|---|---|---|
| WI-01 | Schema versioning + new `ActionType`s + `StepEffect` + `ReplayPolicy` + provenance | `77937c8` | `tests/agent/test_skill_models.py` | Synthetic skill with every new action type validates; `skills/change_title.json` still loads |
| WI-02 | `TraceEvent` identity + causality + ordering metadata | `9876d7c` | `tests/agent/test_trace_causality.py` | Click + caused SPA route share causality; manual nav has `caused_by=None` |
| WI-03 | Richer element/control metadata at record time (`control_kind`, `value_kind`, options snapshot, ARIA expanded/disabled, locale/timezone hints) | `620438a` | `tests/agent/test_skill_models.py` | Catalog form select option/ disabled / date metadata recorded |
| WI-04 | Portal idempotency as capability data (not global injection) | `795d75a` | `tests/agent/test_idempotency.py` | Same endpoint + different body produces different keys; disabled portal injects nothing |
| WI-05 | Typed parameter inference + value codecs (`iso_date`, `localized_number`, `enum_value`, `enum_label`, `file_ref`, `boolean`) | `986da31` | `tests/agent/test_param_codecs.py` | Date/slider/checkbox/select/multiselect map to correct typed params |
| WI-06 | Structured diagnostics replace silent failures (`Diagnostic` model with stable codes) | `9c191e9` | `tests/agent/test_diagnostics_wi06.py` | Bad payload / screenshot / watcher / persistence failures all emit visible diagnostics |
| WI-07 | Runner fail-fast by default with explicit continue policy | `69521b5` | `tests/agent/test_fail_fast_policy.py` | Synthetic skill with failed step 2 never runs step 3 unless `on_failure='continue'` |
| WI-08 | Collapse click-caused SPA navigation into a click effect, not standalone `goto` | `cdf5ff5` | `tests/agent/test_click_navigation_wi08.py` | Replay clicks `btn-open-A-9003`, ends on `/asset/A-9003`, never goto `/asset/A-9001` |
| WI-09 | Action-scoped network/DOM baselines + configurable request-log cap | `bf1c0d3` | `tests/agent/test_action_baselines_wi09.py` | Region change waits for `/api/markets?region=...` after the change; stale prior request can't satisfy a later step |
| WI-10 | Observed busy/readiness signals replace spinner-by-convention | `1092e8f` | `tests/agent/test_readiness_signals_wi10.py` | Save button disabled/enabled transition captured and awaited without `status-saving` naming |
| WI-11 | Provenance-based templates replace substring luck (`TemplatePart`, `ParamProvenance`) | `054f456` | `tests/agent/test_provenance_templates_wi11.py` | `btn-open-A-9001` templates to `btn-open-{asset_id}`, not `btn-open-{search}01` |
| WI-12 | Annotator semantic clustering pipeline (`SemanticCluster`, annotate_mode) | `1d44013` | `tests/agent/test_semantic_clusters_wi12.py` | Typed input changes + Enter + submit produce one `fill_submit` step |
| WI-13 | Preserve clear-field actions as real value transitions (`ValueTransition.clear_intent`) | `6857158` | `tests/agent/test_clear_field_wi13.py` | `title='Old' -> clear -> save` produces fill with value `''` and replays to empty field |
| WI-14 | Click gesture/effect classification (`click_gesture`, `effect_signature`) replaces 200ms dedupe | `3497538` | `tests/agent/test_click_gesture_wi14.py` | Double-click trace produces double-click action; toggles preserved or converted to desired state |
| WI-15 | `fill_submit` collapses input burst + Enter/submit | `95688e2` | `tests/agent/test_fill_submit_wi15.py` | Catalog search for A-9003 emits one fill_submit and one result wait |
| WI-16 | `select_autocomplete` query + result selection | `2446346` | `tests/agent/test_select_autocomplete_wi16.py` | Searching A-90 + selecting A-9003 replays with a different selection |
| WI-17 | `select_option` native single-select with declared aliases, no fuzzy | `f0df00a` | `tests/agent/test_select_option_wi17.py` | If recorded option is absent and no alias declared, runner fails listing available options |
| WI-18 | Cascading select dependency model (Region -> Market -> Language via `DependencyChain`) | `ddd811a` | `tests/agent/test_cascading_select_wi18.py` | Replaying with a different region waits for that region's markets; fails cleanly if recorded market isn't valid |
| WI-19 | `set_selection` auto-collapse for custom MultiSelect | `d492ede` | `tests/agent/test_set_selection_wi19.py` | Recording `[sports, drama]` replays with `[kids]` and final chips equal exactly `[kids]` |
| WI-20 | Nested/searchable/dependent multi-selects (`depends_on`, `parent_picker_fp`, `option_source`) | `62a205b` | `tests/agent/test_dependent_multiselect_wi20.py` | Nested picker replay with a different parent selects only options valid under that parent |
| WI-21 | `date_select` for native + custom date pickers | `a479bd3` | `tests/agent/test_date_select_wi21.py` | Recording one publish date replays a different date without re-driving calendar clicks |
| WI-22 | Ambiguity detection at every locator level (`AmbiguityPolicy`) | `dcb558b` | `tests/agent/test_ambiguity_policy_wi22.py` | Multiple visible 'Open' buttons produce a row-picker pause instead of clicking the first |
| WI-23 | `LocatorProbeResult` replaces `_first_visible` exception masking | `1248308` | `tests/agent/test_locator_probe_wi23.py` | Hidden matching element is rejected with `locator_not_visible`, not clicked |
| WI-24 | Safe selector construction (`_replay_escape_attr`, semantic locators) | `d339560` | `tests/agent/test_selector_escape_wi24.py` | IDs/names containing `]`, quotes, spaces, colons resolve correctly |
| WI-25 | Declared aliases + structural failure replace automatic fuzzy | `aa2025f` | `tests/agent/test_select_aliases_wi25.py` | Recorded 'US' never auto-selects 'UAE' unless alias declared |
| WI-26 | L3 locator repair around uniqueness + evidence (`RepairPolicy`) | `37176d3` | `tests/agent/test_repair_policy_wi26.py` | Two similar 'Approve' buttons force ambiguity even if one has high text similarity |
| WI-27 | Action-specific postconditions replace L3 whole-page signature (`StepAssertion` expanded) | `399dbd2` | `tests/agent/test_action_assertions_wi27.py` | L3 click that changes unrelated text but not the expected field/status fails verification |
| WI-28 | `slider_set` captures + replays range sliders | `ef3bb79` | `tests/agent/test_slider_set_wi28.py` | Moving slider emits one `slider_set`; replay sets a different value and verifies it |
| WI-29 | Strengthen file upload modeling (`FileSpec`, `FileMetadata`, validation) | `e09db22` | `tests/agent/test_file_upload_wi29.py` | Replay with different file path succeeds when constraints match; fails before mutation when file missing |
| WI-30 | `drag_drop` recording + replay | `0cc11d4` | `tests/agent/test_drag_drop_wi30.py` | Dragging item to drop zone records/replays and asserts target contains the dragged item |
| WI-31 | LLM annotate enriches STRUCTURE via validated overlays | `552cf95` | `tests/agent/test_llm_structural_overlays_wi31.py` | LLM can rename params + confirm detected clusters; cannot invent dependencies without trace evidence |
| WI-32 | Iframe + shadow DOM traversal (`FrameStep`) | `0f87c1b` | `tests/agent/test_iframe_shadow_wi32.py` | Element inside same-origin iframe and inside shadow root can be recorded + replayed |
| WI-33 | Accordion / collapse as DESIRED state (`toggle_state`) | `74fe1a0` | `tests/agent/test_toggle_state_wi33.py` | Replay leaves panel expanded whether it starts collapsed or already expanded |
| WI-34 | Modal open/close effects folded onto causing click (`ModalEffect`) | `51f6b9c` | `tests/agent/test_modal_effect_wi34.py` | Modal close via Escape replays as global Escape and verifies dialog hidden |
| WI-35 | Popup / new-window workflows captured + bound to page registry (`PopupEffect`) | `7dcb963` | `tests/agent/test_popup_effect_wi35.py` | Clicking a button that opens a popup causes runner to switch and execute next step there |
| WI-36 | Auth preconditions on steps that hit protected endpoints (`AuthPrecondition`) | `ccc9d42` | `tests/agent/test_auth_precondition_wi36.py` | Recording login doesn't store plaintext password; business replay pauses for auth |
| WI-37 | Virtualized / paginated tables as `scroll_until` | `e518b97` | `tests/agent/test_scroll_until_wi37.py` | Replay selects row by asset ID even when it starts on another page or outside virtualized viewport |
| WI-38 | `scroll_until` signal-backed, not magic counts | `5190159` | `tests/agent/test_scroll_until_signal_wi38.py` | Infinite-scroll list replay scrolls until target row key is visible, then clicks |
| WI-39 | Contenteditable / rich-text editor as one semantic step (`rich_text_set`) | `3db98dd` | `tests/agent/test_rich_text_set_wi39.py` | Recording a contenteditable note replays a different note and verifies editor content |
| WI-40 | Hover-driven menus + mega-menus as `effects.hover` | `b0d21c4` | `tests/agent/test_hover_effect_wi40.py` | Mega-menu item replay succeeds only after hover reveals its container |
| WI-41 | Global keyboard shortcuts as their own action | `9726f0b` | `tests/agent/test_shortcut_wi41.py` | Ctrl+S records/replays + verifies save signal |
| WI-42 | Toast-driven undo + conflict surfaced structurally (`ToastEffect`) | `5eb0265` | `tests/agent/test_toast_effect_wi42.py` | Save-conflict toast pauses or follows declared resolution instead of silently continuing |
| WI-43 | Disabled-until-enabled readiness as first-class wait | `0d44bcb` | `tests/agent/test_field_enabled_wi43.py` | File input enabled after content selection is awaited explicitly before upload |
| WI-44 | Server validation errors as classifiable signals (`StepAssertion.kind='validation_field'`) | `6f2beb1` | `tests/agent/test_server_validation_wi44.py` | Rejected submit surfaces field-specific validation details and stops replay |
| WI-45 | Download/export workflows captured + replayed via `expect_download` (`DownloadEffect`, `DownloadSpec`) | `b67d8c6` | `tests/agent/test_download_wi45.py` | Export click produces a downloaded file artifact path in replay result |
| WI-46 | Cross-tab / multi-window: `PageContext` routes steps to popup pages | `871615a` | `tests/agent/test_page_context_wi46.py` | Workflow opens preview tab, verifies preview, returns to original tab, continues |
| WI-47 | Push frames (WebSocket / SSE) as first-class `expected_signals.push` | `2c0a446` | `tests/agent/test_push_expectation_wi47.py` | SSE-delivered state update satisfies a declared signal; generic idle does not wait forever |
| WI-48 | Locale/timezone normalization through codec + `strict_locale` gate (`RecordingContext`, locale_mismatch) | `d4a7db9` | `tests/agent/test_locale_normalization_wi48.py` | Date recorded in one display format replays correctly under declared portal timezone/locale |
| WI-49 | Canvas / SVG / media adapter-driven gestures + `noop_click` sample | `1f47854` | `tests/agent/test_canvas_gesture_wi49.py` | `canvas_gesture` step with `adapter_name='noop_click'` records + replays |
| WI-50 | Final regression matrix + skill upgrader + CLI | `745c0c3`, `45e6adb` | `tests/agent/test_skill_upgrade.py`, `tests/agent/test_wi_regression_matrix.py` | Automated matrix covers every WI; legacy skills upgrade idempotently to v2 |

All fifty WIs landed. None were skipped.

## Audit hardcoded-heuristic table resolution

Source: heuristic table in
[`2026-05-21_teach-replay-audit.md`](2026-05-21_teach-replay-audit.md).

| Audit row | What was wrong | Where it's now | Status |
|---|---|---|---|
| Consecutive same-URL navigate dedupe uses `last_nav_url` | Intentional reloads dropped | F-05 + WI-08 + WI-12 keep raw navs; annotator classifies via causality graph | CLOSED |
| Empty input after prior same target is dropped | Clear-and-save disappears | WI-13 `ValueTransition.clear_intent` | CLOSED |
| Duplicate click `<200ms` dropped | Double-click / repeat clicks lost | WI-14 `click_gesture` classification (single/double/repeat/toggle/open/close) | CLOSED |
| Param inference only for `input_change` / `file_selected` | Dates / bools / lists / enums become strings | WI-05 typed `SkillParam` + codecs | CLOSED |
| Template derivation uses substring replace `len >= 3` | `A-90` inside `A-9001` -> `btn-open-{q}01` | WI-11 `TemplatePart` + `ParamProvenance` (row_key / route_param / request_param) | CLOSED |
| Navigate URL is never templated | `/asset/A-9001` remains literal | WI-08 `NavigationEffect.url_template` + WI-11 provenance-driven templating | CLOSED |
| Select fuzzy threshold `0.7` | `US` could match `UAE` | WI-17 + WI-25 declared aliases; `option_match_policy='exact_only'` default | CLOSED |
| Ambiguity only fires for templated `test_id` / `element_id` | L2 role/name matches silently use `.first` | WI-22 `AmbiguityPolicy` at every locator level | CLOSED |
| `_first_visible` catches all exceptions then uses `count() > 0` | Hidden/strict-mode failures become "clickable" | WI-23 `LocatorProbeResult` with structured states | CLOSED |
| CSS escape only handles `\` and `'` | IDs with `]`, spaces, colons break | WI-24 `_replay_escape_attr` + Playwright semantic locators | CLOSED |
| Generic settle numbers (250ms / 2s / 4s / 1.5s) | Slow enterprise APIs race | WI-09 action-scoped baselines + WI-10 readiness signals; defaults remain as documented fallbacks in `PortalContext.wait_policy` per F-08c | CLOSED |
| Spinner selector list is convention-based | Portals use arbitrary loading markup | WI-10 observed-readiness signals; spinner selector remains legacy fallback only | CLOSED |
| Request log cap `50` | Dashboards >50 calls evict target | WI-09 action-scoped request registry + configurable cap; default surfaced in `PortalContext.wait_policy` | CLOSED |
| L3 repair thresholds `0.85/0.65/0.55` | Repeated similar buttons drift | WI-26 `RepairPolicy` (uniqueness scope + required postcondition + required features) | CLOSED |
| L3 verification uses URL/text length/interactable count | Spinner change passes wrong click | WI-27 `StepAssertion` action-specific kinds (`url_matches_template`, `field_value_equals`, `selection_equals`, `toast_visible`, `request_completed`, `download_started`, `validation_field`) | CLOSED |
| `page.goto(...networkidle...)`, second `goto` on timeout | Long-polling portal reloads twice | WI-08: caused navigation never calls `page.goto`; standalone nav uses one attempt | CLOSED |
| Idempotency key strips query/hash only | Same endpoint + different body dedupes | WI-04 + F-02 + F-03 + F-04: capability data, configurable header, body hash fields, fetch+XHR seeded correctly | CLOSED |

All seventeen hardcoded-heuristic rows are CLOSED. Each replacement
is cited above with the WI commit + test file.

## 17-interaction matrix coverage

| # | Interaction | Covered by | Status |
|---|---|---|---|
| 1 | Text + Enter submit | WI-15 `fill_submit` | COVERED |
| 2 | Search + dropdown results | WI-16 `select_autocomplete` | COVERED |
| 3 | Native select | WI-17 `select_option` + WI-25 aliases | COVERED |
| 4 | Cascading selects | WI-18 `DependencyChain` | COVERED |
| 5 | Custom multi-select | WI-19 `set_selection` auto-collapse | COVERED |
| 6 | Nested dropdown multi-select | WI-20 `depends_on` + `parent_picker_fp` | COVERED |
| 7 | Date picker | WI-21 `date_select` (native + custom) | COVERED |
| 8 | Slider | WI-28 `slider_set` | COVERED |
| 9 | File upload | WI-29 `FileSpec` + validation | COVERED |
| 10 | Drag/drop | WI-30 `drag_drop` | COVERED |
| 11 | Accordion | WI-33 `toggle_state` (desired state) | COVERED |
| 12 | Modal open/close | WI-34 `ModalEffect` (folded on click) | COVERED |
| 13 | Click navigates | WI-08 click + navigation effect | COVERED |
| 14 | Popup/new window | WI-35 `PopupEffect` + WI-46 `PageContext` | COVERED |
| 15 | Auth workflows | WI-36 `AuthPrecondition` (no plaintext password capture) | COVERED |
| 16 | Virtualized/paginated tables | WI-37 + WI-38 `scroll_until` (signal-backed) | COVERED |
| 17 | Scroll | WI-38 `scroll_until` only when meaningful | COVERED |

All 17 rows COVERED. Each has a dedicated WI commit and test file
listed in the WI table above.

## Outstanding risks / deferred work

None of the 50 WIs were deferred; all landed during the sprint. Items
the batches surfaced as risks but did not block on:

- **WI-31 LLM structural overlays.** The LLM enrichment layer can rename
  params and confirm clusters but is gated by validation against raw
  trace evidence. Operators who hand-edit the v2 sidecar JSON can still
  introduce structurally-unsupported claims; the validation gate flags
  these but does not auto-correct them. (`tests/agent/test_llm_structural_overlays_wi31.py`)

- **WI-32 iframe / shadow DOM** assumes same-origin iframes. Cross-
  origin iframes require Playwright's `frame_locator` with a CDP attach;
  the current implementation falls back to a deferred-step diagnostic
  rather than failing the whole skill. Will need a follow-up when a
  real enterprise portal trips it.

- **WI-35 popup / WI-46 cross-tab** workflows handle `window.open` and
  target=_blank. Workflows that span pre-existing tabs the operator
  opened manually (i.e. the runner discovers, not opens) are out of
  scope and remain on the page binding the orchestrator set at start.

- **WI-43 readiness / WI-10 busy signals** rely on the grabber
  capturing the disabled/enabled or aria-busy transition during the
  recording. Portals whose readiness signal is purely intrinsic
  (e.g. a Promise the page awaits without any DOM/network reflection)
  still need a portal-context-declared wait.

- **WI-47 push / WebSocket / SSE**: matching is body-substring based.
  Binary frames are not introspected. Portals with structured binary
  protocols need a portal-specific message decoder.

- **WI-49 canvas adapters**: only the sample `noop_click` adapter ships
  in this sprint. Real portal canvas/SVG/media flows will need
  portal-specific adapters registered via `pilot.adapters.register_canvas_adapter`.

- **F-06 `key_dom_signature`** is a coarse hash. Portals where two
  unrelated states share the same ancestor BoundingClientRect can have
  false positives. The annotator treats it as a hint, not a hard
  matcher, so the risk is bounded.

- **F-08c wait defaults** (`5000ms` step assertion timeout, `4000ms`
  legacy assertion timeout, `250ms` DOM stable window) remain as
  fallbacks. New annotations populate them explicitly from
  `PortalContext.wait_policy`; legacy skills accept the defaults. We
  did not delete the fallback constants because removing them silently
  breaks skills that don't set wait_policy.

- **Smoke verification**: PASSED. `tmp/smoke-2026-05-21/` carries the
  full agent-browser transcript + final screenshot. The sample portal
  catalog -> search -> open -> edit title -> save flow works end-to-end
  with no regressions from the structural fix sprint. See
  `tmp/smoke-2026-05-21/README.md` for the artifact index.

## How to upgrade legacy skills

A legacy skill (no `schema_version`, no `replay_policy`, no `effects`)
is upgraded with:

```
python -m pilot upgrade-skill skills/my_old_skill.json
```

The upgrader is idempotent: re-running on an upgraded skill is a no-op.
It preserves legacy continue-on-failure (`on_failure='continue'`) so
pre-WI-07 flows don't start aborting mid-stream.

Example before (v1):

```json
{
  "name": "change_title",
  "version": 1,
  "params": [{"name": "input_search", "type": "string", "required": true}],
  "steps": [
    {"index": 0, "action": "navigate", "url": "http://localhost:5188/catalog"},
    {"index": 1, "action": "change", "fingerprint": {"test_id": "input-search"}, "value": "A-9001"}
  ]
}
```

After (v2):

```json
{
  "name": "change_title",
  "version": 1,
  "schema_version": 2,
  "upgraded_from": 1,
  "params": [{"name": "input_search", "type": "string", "required": true, "codec": "raw", "constraints": null, "provenance": null}],
  "steps": [
    {"index": 0, "action": "navigate", "url": "http://localhost:5188/catalog", "replay_policy": {"on_failure": "continue", ...}, "effects": null, "provenance": null, ...},
    {"index": 1, "action": "change", "fingerprint": {"test_id": "input-search"}, "value": "A-9001", "replay_policy": {"on_failure": "continue", ...}, ...}
  ]
}
```

The upgrader is `pilot.skill_upgrade.upgrade_skill_to_v2(skill_json: dict) -> dict`.
File-level helper is `pilot.skill_upgrade.upgrade_skill_file(path: Path)`.

## Test counts

- Baseline at start of sprint (operator-reported): 430 passing.
- This sprint added:
  - 9 tests in `tests/agent/test_skill_upgrade.py` (WI-50 upgrader).
  - 60 tests in `tests/agent/test_wi_regression_matrix.py` (WI-50 matrix).
- Final: 499 passing, 0 failing.
- Ignored: `tests/agent/test_groq_live.py` (live LLM, network-gated).

```
.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/agent/test_groq_live.py -q
> 499 passed, 640 warnings in 2.26s
```

## Pointers

- Plan: [`2026-05-21_fix-everything-plan.md`](2026-05-21_fix-everything-plan.md)
- Audit: [`2026-05-21_teach-replay-audit.md`](2026-05-21_teach-replay-audit.md)
- Foundation audit: [`2026-05-21_foundation-verification.md`](2026-05-21_foundation-verification.md)
- Structural engineer guide: [`../STRUCTURAL_FIX_GUIDE.md`](../STRUCTURAL_FIX_GUIDE.md)
- Changelog entry: [`../CHANGELOG.md`](../CHANGELOG.md) (2026-05-21)
