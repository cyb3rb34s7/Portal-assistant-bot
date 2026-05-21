# CurationPilot Deep Audit Report

I audited the requested teach / annotate / schema / replay / orchestrator / sample-portal paths. No files were modified.

## BLOCKERS

### 1. Click-caused SPA navigation is modeled as an independent `navigate` step

Today the grabber emits a normal click (`pilot/overlay/grabber.js:355-378`) and independently emits every history route change as `navigate` (`pilot/overlay/grabber.js:625-650`). Annotate then maps every `navigate` event to a separate `SkillStep.url` (`pilot/annotate.py:153-160`, `pilot/annotate.py:229-233`). Runner executes that with `page.goto()` (`pilot/skill_runner.py:307-328`).

The current generated skill already shows the bug shape:

- Step 6 click: `btn-open-A-9001`, badly templated to `btn-open-{input_catalog_search}01` (`skills/change_title.json:479-543`)
- Step 7 standalone navigate: literal `/asset/A-9001` (`skills/change_title.json:563-580`)

This is a release blocker. Even when the URL is correctly templated, `page.goto()` is not a harmless verification; it reloads the SPA and replays initial fetches.

**Minimum fix:** collapse click + caused route change into one step. Do **not** drop based on time. Correlate the route change as an effect of the click and preserve raw trace causality.

Recommended schema shape:

```json
{
  "index": 6,
  "action": "click",
  "semantic_label": "open_catalog_asset",
  "fingerprint": {
    "test_id": "btn-open-A-9001",
    "templates": {
      "test_id": "btn-open-{asset_id}"
    }
  },
  "effects": {
    "navigation": {
      "kind": "spa_route",
      "url_template": "http://localhost:5188/asset/{asset_id}",
      "source": "history.pushState",
      "timeout_ms": 3000,
      "reload_allowed": false
    }
  },
  "assert_after": [
    {
      "kind": "url_matches_template",
      "template": "/asset/{asset_id}"
    },
    {
      "kind": "visible",
      "selector": "[data-testid='asset-page']"
    }
  ]
}
```

Runner behavior: click, wait for URL match / route-state match, never call `goto()` for caused navigation.

---

### 2. `expected_signals` exists in schema but is not populated, and runner waits at the wrong time

The schema correctly says expected signals are per-step waits after an action, e.g. after Region wait for `/api/markets` (`pilot/skill_models.py:178-190`). But annotate never populates `expected_signals`; its `SkillStep(...)` construction only carries action/fingerprint/url/value/file/gate metadata (`pilot/annotate.py:229-239`). Runner then calls `_wait_for_page_settle(expected=step.expected_signals)` **before** executing the step (`pilot/skill_runner.py:227-238`).

That makes cascading dropdowns structurally wrong: Region’s `/api/markets` wait belongs after selecting Region, not before selecting Region.

**Minimum fix:**

- Annotator must produce `expected_signals`.
- Runner must execute action first, then wait for `step.expected_signals`.
- Missing required expected network/DOM signals must fail the step, not just warn (`pilot/skill_runner.py:1629-1637`).

---

### 3. Input bursts and widget workflows are not collapsed into semantic actions

Text input is debounced at 400ms (`pilot/overlay/grabber.js:23`, `pilot/overlay/grabber.js:393-424`). If the operator pauses, multiple `input_change` events become multiple `change` steps with the same param. The current skill shows repeated `fill_input_catalog_search` steps (`skills/change_title.json:120-224`) before Enter (`skills/change_title.json:296-306`).

Multi-select support exists in schema (`pilot/skill_models.py:217-255`) and runner (`pilot/skill_runner.py:502-699`), but annotate does not produce `set_selection`. Therefore categories/tags still replay as a brittle sequence of open/search/check/close clicks.

**Minimum fix:** annotate must coalesce event clusters:

- Text burst + Enter + submit → one `submit_search(query_param)` or `fill_and_submit`.
- Multi-select open/search/check/check/close → one `set_selection` step.
- Native select sequences → typed `select_value` steps with option metadata.

---

### 4. Current waits can falsely satisfy while the page is not ready

Key race points:

- Watcher install silently fails; then `__cp_inflight || 0` makes the page look idle (`pilot/skill_runner.py:1371-1380`, `pilot/skill_runner.py:1576-1585`).
- If grabber already installed `__cp_quiescence_installed`, runner watcher exits before installing `__cp_request_log` (`pilot/skill_runner.py:1285-1293` vs. grabber’s older globals at `pilot/overlay/grabber.js:33-40`).
- Expected network waits search the whole request log with no “started after this action” boundary (`pilot/skill_runner.py:1609-1623`), so stale requests can satisfy future steps.
- `options_changed` checks only count stability, not option identities; EMEA and APAC both having four markets can false-pass (`pilot/skill_runner.py:1651-1687`).
- Generic wait timeouts are swallowed (`pilot/skill_runner.py:1525-1556`).

**Minimum fix:** every wait predicate needs an action-scoped baseline: `started_at`, pre-action option signature, post-action required request IDs, and hard failure for required misses.

---

### 5. Runner continues after sub-step failure

`SkillRunner.run()` logs failure but keeps executing subsequent steps (`pilot/skill_runner.py:175-178`). In an automation system that can save/publish content, continuing after a failed locator or stale navigation can mutate the wrong entity.

**Minimum fix:** default to fail-fast for replay skills. Allow explicit `on_failure: continue` only for annotated optional steps.

---

## MAJOR

### Hardcoded heuristic audit

| Heuristic | Citation | Real portal break | Better alternative |
|---|---:|---|---|
| Consecutive/same URL navigate dedupe uses `last_nav_url`, not true adjacency | `pilot/annotate.py:76-83` | Intentional refresh or return to same route is dropped | Keep all raw navs; annotator classifies as initial / caused / explicit / duplicate with cause IDs |
| Empty input after prior same target is dropped | `pilot/annotate.py:85-89` | “Clear field and save” disappears | Preserve clear as semantic value transition `old -> ""` |
| Duplicate click `<200ms` dropped | `pilot/annotate.py:93-101` | Double-click open, grid cell edit, toggle accordion twice | Use DOM effect classification, not time-only dedupe |
| Param inference only for `input_change` / `file_selected`; all values become string/file | `pilot/annotate.py:164-181` | dates, booleans, lists, enum options are mis-modeled | Infer control type from fingerprint + DOM metadata; typed params |
| Template derivation uses substring replace, `len >= 3` | `pilot/annotate.py:299-340` | `A-90` inside `A-9001` becomes `btn-open-{q}01` | Segment-aware templates from DOM attributes and known row key, not arbitrary substrings |
| Navigate URL is never templated | `pilot/annotate.py:231` | `/asset/A-9001` remains literal | Add `url_template` / navigation effect templating |
| Select fuzzy threshold `0.7` | `pilot/skill_runner.py:989-1052` | “US” can fuzzy-match wrong locale/status option | No fuzzy auto-select unless annotated alias map; otherwise fail with available options |
| Ambiguity only fires for templated `test_id` / `element_id` | `pilot/skill_runner.py:913-945` | L2 “Open” matches many rows and `.first` wins | Ambiguity detection for every locator level when candidate count > 1 |
| `_first_visible` catches all exceptions then uses `count() > 0` | `pilot/skill_runner.py:1948-1957` | Hidden/strict-mode failures become clickable candidates | Return structured locator diagnostic; never convert visibility failure into success |
| CSS escape only handles `\` and `'`; `[name='...']` is unescaped | `pilot/skill_runner.py:1137-1142`, `pilot/skill_runner.py:1944-1945` | IDs/names with `]`, spaces, quotes, colons break selectors | Use Playwright locators or browser `CSS.escape` via evaluate |
| Generic settle numbers: 250ms quiet, 2s DOM, 4s total, 1.5s spinner | `pilot/skill_runner.py:1492-1556` | slow enterprise APIs, debounced SPAs, long-polling | Step-specific signals and portal-configured wait budgets |
| Spinner selector list is convention-based | `pilot/skill_runner.py:1261-1268` | portals use arbitrary loading markup | Annotate observed “busy” elements or ARIA busy/progress relationships |
| Request log cap 50 | `pilot/skill_runner.py:1292-1297` | dashboards fire >50 telemetry/API calls and evict target | Action-scoped request registry, not global ring buffer |
| L3 repair thresholds 0.85/0.65/0.55 | `pilot/agent/locator_repair.py:185-191` | repeated “Approve” buttons across cards | Require candidate uniqueness + postcondition, not score alone |
| L3 verification uses URL/text length/interactable count | `pilot/skill_runner.py:1785-1833` | spinner text change passes wrong click; silent save fails | Verify annotated postcondition or action-specific signal |
| `page.goto(...networkidle...)`, then second `goto` on timeout | `pilot/skill_runner.py:324-328` | long-polling portal reloads twice | Use one navigation attempt; fallback to current-load-state wait, not a second goto |
| Idempotency key strips query/hash only | `pilot/skill_runner.py:1396-1416` | same endpoint different body dedupes incorrectly | Include method + canonical body hash + selected semantic params; make opt-in per portal |

---

## MINOR

### Silent failures that should surface

The code has many `except Exception: pass/return None` sites. The most user-visible ones:

- Teach silently drops bad payload JSON and invalid fingerprints (`pilot/teach.py:188-206`).
- Page snapshots are dropped when `portal_id` is absent or catalog merge fails (`pilot/teach.py:236-263`).
- Screenshot failures disappear (`pilot/teach.py:279-286`, `pilot/skill_runner.py:1912-1916`).
- Runner watcher install failures disappear (`pilot/skill_runner.py:1371-1380`).
- Set-selection search, remove, and commit failures are swallowed (`pilot/skill_runner.py:623-688`).
- Ambiguity detection suppresses locator exceptions (`pilot/skill_runner.py:933-954`).
- Executor hint/alternate persistence failures mostly return false/zero (`pilot/agent/executor_real.py:550-582`, `pilot/agent/executor_real.py:586-639`).

These should at least emit structured diagnostics into the replay event stream, not just audit logs.

### Schema ahead of implementation

`frame_path` / `in_shadow_root` exist (`pilot/skill_models.py:69-71`), but grabber always records `frame_path: []` (`pilot/overlay/grabber.js:320-321`) and runner locators do not traverse frames. This will fail common enterprise portals with embedded DAM/SSO/preview iframes.

---

## STRATEGIC

### Wrong-layer assumptions

1. **Grabber is deciding semantic units too early.**  
   The 400ms text debounce is a recorder policy, but “what constitutes one fill” belongs in annotate. Recorder should capture raw input transitions with timestamps; annotate should collapse.

2. **Annotator encodes templates as substring luck.**  
   Template derivation (`pilot/annotate.py:270-340`) should be based on known param provenance: row key, selected option, route param, request param. Substring replacement is not safe.

3. **Runner is re-deriving semantics that should be data.**  
   Fuzzy select, ambiguity, spinners, set-selection behavior, and postconditions should be in the skill. Runner should execute declared structure, not infer portal intent at replay time.

4. **Idempotency injection is too global.**  
   Mutating all non-GET requests with `Idempotency-Key` (`pilot/skill_runner.py:1382-1455`) is a powerful safety net, but it assumes backend semantics. Make it portal capability data: enabled endpoints, key shape, body hashing, retry policy.

5. **LLM annotate enriches names, not structure.**  
   `annotate_llm.py` only proposes parameter names, preconditions, destructive flags, and success assertions (`pilot/agent/annotate_llm.py:117-260`). It does not populate `depends_on`, `expected_signals`, or `set_selection`. The structural pass should be deterministic first, LLM-assisted second.

---

## 17 Interaction Dry-Run Matrix

| # | Today | Break | Minimum fix |
|---:|---|---|---|
| 1. Text + Enter submit | Debounced `input_change`, `key`, `submit` (`grabber.js:393-516`) | Multiple fills, Enter/submit duplication, submit implicit success | Collapse to `fill_and_submit`, one final value, explicit submit trigger |
| 2. Search + dropdown results | Fill then click rendered result | Network/result timing not associated | `select_autocomplete` with query, result identity, network wait, result container |
| 3. Native select | Captures selected value (`grabber.js:475-480`) | Dynamic options + fuzzy wrong pick | Store value+label+option list; fail if requested option unavailable |
| 4. Cascading selects | Region/Market/Language are independent changes | Recorded Market invalid under new Region | Populate `depends_on`, expected network, option-signature waits |
| 5. Custom multi-select | Open/search/check events linearized | Cannot replay different set size | Auto-collapse to `set_selection` |
| 6. Nested dropdown multi-select | Same, plus dependency/network absent | Parent-constrained options stale | `set_selection` with option source and `depends_on` |
| 7. Date picker | Native date captured; custom calendar is clicks | Different target date needs different pattern | Date param + widget adapter / calendar semantics |
| 8. Slider | Comment says range, code does not capture range (`grabber.js:427-500`) | No trace | Capture range input/change; runner set value + dispatch events |
| 9. File upload | Captures filename only (`grabber.js:482-490`) | Replay needs different path/shape | Capture file metadata; require replay `file_path`; validate MIME/size if needed |
| 10. Drag/drop | Uncovered | No trace | Add `drag_drop` action: source/target/dataTransfer |
| 11. Accordion | Normal click | Already expanded becomes collapsed | Desired state assertion: click only if `aria-expanded`/panel state differs |
| 12. Modal open/close | Open click; close may be root click/Esc only on input | Fragile backdrop/root target | Modal effects: dialog visible/hidden, global Escape, close button |
| 13. Click navigates | Click + standalone navigate | Wrong asset / reload race | Navigation effect on click; no duplicate goto |
| 14. Popup/new window | Uncorrelated | Runner does not expect/switch page | Capture `opens_page`; runner `expect_popup` and bind new page |
| 15. Auth workflows | Preflight exists (`executor_real.py:119-223`); login can be recorded | Password fields are textish; auth mixed with business skill | Redact password; model auth as precondition/bootstrap |
| 16. Virtualized/paginated tables | Row-specific click | Row absent/offscreen/wrong page | Collection query model: search/sort/page/scroll until row key |
| 17. Scroll | Explicitly filtered (`grabber.js:7-9`) | Infinite/virtual scroll not hydrated | Capture scroll only when it causes DOM/network; model `load_until_visible` |

---

## Recommended End-State: Click + Navigate

### Grabber

Pushback: correlating in grabber is right, but **do not only suppress based on 500ms**. Keep raw events with causality.

```js
let activeInteraction = null;

document.addEventListener("click", (e) => {
  const id = crypto.randomUUID();
  activeInteraction = { id, kind: "click", ts: performance.now() };

  post({
    id,
    kind: "click",
    fingerprint: fingerprint(target),
    page_url: location.href
  });

  queueMicrotask(() => {
    // Keep available briefly for sync SPA routers.
    setTimeout(() => {
      if (activeInteraction?.id === id) activeInteraction = null;
    }, 3000);
  });
}, true);

function postNavigate(source) {
  post({
    id: crypto.randomUUID(),
    kind: "navigate",
    url: location.href,
    page_url: location.href,
    caused_by: activeInteraction?.id || null,
    navigation_source: source
  });
}
```

### Annotator

If `navigate.caused_by == click.id`, fold it into the click step as an effect. If cause is absent, emit a standalone `navigate`.

### Runner

For click with navigation effect:

1. Capture current URL.
2. Click.
3. Wait for URL/template match and declared DOM/network signals.
4. Assert route.
5. Never call `page.goto()`.

---

## Recommended End-State: Cascading Region → Market → Language

Sample portal proves the shape: Region change fires `/api/markets?region=...` (`sample_portal/src/pages/AssetDetail.jsx:78-90`); Market change fires `/api/languages?market=...` (`sample_portal/src/pages/AssetDetail.jsx:92-103`). Backend latencies make the race real (`sample_portal/mock_backend.js:260-270`).

Skill shape:

```json
{
  "params": [
    { "name": "region", "type": "string" },
    {
      "name": "market",
      "type": "string",
      "depends_on": "region",
      "select_from_current_options": true
    },
    {
      "name": "language",
      "type": "string",
      "depends_on": "market",
      "select_from_current_options": true
    }
  ],
  "steps": [
    {
      "action": "change",
      "semantic_label": "select_region",
      "fingerprint": { "test_id": "select-region" },
      "param_binding": { "name": "region", "type": "string", "mode": "whole" },
      "expected_signals": {
        "network": [
          { "method": "GET", "url_pattern_template": "/api/markets?region={region}", "status": 200 }
        ],
        "dom": [
          {
            "kind": "options_signature_changed",
            "selector": "[data-testid='select-market']"
          }
        ]
      }
    }
  ]
}
```

Runner should use current option values. If `market` is not present after selecting `region`, fail structurally with `dependent_option_unavailable`, listing available values. Do not fuzzy-pick.

---

## Recommended End-State: Categories + Tags Multi-Select

The widget has stable structure: toggle/search/checkbox/chip prefixes (`sample_portal/src/components/MultiSelect.jsx:57-129`). Annotator should detect same-prefix clusters and collapse:

```json
{
  "index": 9,
  "action": "set_selection",
  "semantic_label": "set_categories",
  "set_selection": {
    "mode": "replace",
    "param": "categories",
    "open_picker_fp": {
      "test_id": "multiselect-categories-toggle"
    },
    "search_fp": {
      "test_id": "multiselect-categories-search"
    },
    "checkbox_template_fp": {
      "test_id": "multiselect-categories-checkbox-sports",
      "templates": {
        "test_id": "multiselect-categories-checkbox-{item}"
      }
    },
    "current_items_selector": "[data-testid^='multiselect-categories-chip-']",
    "current_items_id_attr": "data-testid",
    "current_items_id_prefix": "multiselect-categories-chip-"
  }
}
```

Runner must verify final chips equal the target set. Current runner can return success without verifying after add/remove (`pilot/skill_runner.py:692-699`).

---

## What You Missed

Model these soon:

- `contenteditable` / rich text editors.
- Iframes and shadow DOM.
- Hover menus and mega-menus.
- Global keyboard shortcuts.
- Toast-driven undo / conflict-resolution flows.
- Disabled-to-enabled readiness.
- Server validation errors bound to fields.
- Download / export workflows.
- Cross-tab workflows.
- WebSocket/SSE-driven state changes.
- Locale/timezone-sensitive dates and numbers.
- Canvas/SVG/media timeline controls.

## Fix First

1. Navigation effects on click; remove duplicate `goto`.
2. Move `expected_signals` waits after actions and make required misses fail.
3. Annotator coalescing: text submit, native cascading selects, multi-select.
4. Action-scoped network/DOM baselines.
5. Fail-fast runner default with structured UI-visible diagnostics.
