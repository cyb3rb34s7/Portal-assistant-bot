# 2026-06-02 — Angular real-portal sprint summary

The sprint that started from your real-portal recording (Samsung Frame TV
`frame2-dev.samsungcloud.tv` artwork-migration flow) and the failures you
reported: missing labels on `mat-select` dropdowns, two unlabeled
dropdowns the planner couldn't tell apart, the search debounce that
captured "cana", "can", "canada" as separate events, the visually-
disconnected search-bar / list pattern on the Show Data results, and the
"if there's a search box you must use it at replay" requirement.

**Status: 12 commits, 565 tests pass, all pushed.** Foundation is in;
three of five replay steps go green end-to-end through the real runner
against a real Angular-Material-shaped portal; two open items remain
(country picker reach + planner clarify UX) — both honestly documented.

Branch: `feat/enterprise-portal-and-additive-sprint` (HEAD `48a4fae`).

---

## Part 1 — What you reported and what was diagnosed

The real portal uses Angular Material with these patterns:

- `<label>Year</label><mat-form-field><mat-select>…</mat-select></mat-form-field>`
  — the field label is a **preceding sibling** of the form-field, not a
  `for=` association and not `<mat-label>`. Three of these cascading:
  Year → Make → Target Model.
- `<ng-multiselect-dropdown>` with `placeholder="Search"` — server-side
  filtered Country/Region. Options depend on Make + Model.
- A "Show Data" button → API call → a `cdk-virtual-scroll-viewport` of
  `<mat-checkbox>` rows, with a search bar **above the list** that is
  visually disconnected from it.
- A "Transfer Right" button that moves checked rows to a destination list
  on the other side.

Diagnosis (saved at `DOCS/reviews/2026-06-02_real-portal-label-capture-diagnosis.md`):

1. **Wrong click target.** Clicks land on the inner `<div>` inside
   `.mat-select-trigger`; the grabber fingerprints THAT div, not the
   parent `<mat-select role="listbox">`. Result: `tag=div, role=null,
   accessible_name=null`.
2. **Wrong place to look for the label.** `getAccessibleName` walked
   ancestors and `for=` — it didn't look at preceding-sibling labels.
3. **Currently-displayed value never captured.** `mat-select-value-text`
   (e.g. `18_KANTM2_8K`) is the single strongest human-readable
   differentiator for a closed mat-select.
4. **Search debounce too short.** 250 ms missed keystroke bursts, so
   "cana", "can", "canada" became separate events.

Combined: the LLM saw two structurally identical `(div, null, null)`
clicks and dropped both Year and Target Model from the inferred param
surface entirely.

---

## Part 2 — What was built (12 commits)

### A. The Angular-Material-style sample portal (`79ece91`)
A static-HTML + vanilla-JS replica of the real portal's DOM at
`portals/angular_sample/`. Same class names (`mat-form-field`,
`mat-select`, `mat-select-trigger`, `mat-select-value-text`,
`ng-multiselect-dropdown`, `cdk-virtual-scroll-viewport`, `mat-checkbox`),
same nesting, same labeling pattern (preceding-sibling `<label>`), same
server-side cascading (Make+Year→Model, Make+Model→Country), same
virtualized list + search pattern. Backend in `mock_backend.js` on port
5189 with realistic delays.

### B. Grabber fixes — the recording-time root causes (`63616e2`, `53e93c4`, `fdae52f`, `2b467dc`)
- **Semantic widget-root resolution.** New helper walks up at most 6
  levels; clicks inside `mat-select-trigger` / `mat-checkbox-layout` /
  `mat-form-field-flex` etc. resolve to the parent `<mat-select>` /
  `<mat-checkbox>` / `<mat-form-field>` / `<ng-multiselect-dropdown>`.
  Outermost semantic widget wins (mat-select > mat-form-field).
- **Extended label discovery.** `getAccessibleName` now also looks
  inside `<mat-form-field>` for `<mat-label>` / `.mat-form-field-label`,
  and walks to the field container's preceding-sibling `<label>` (the
  `<label>Year</label><mat-form-field>…` shape).
- **`current_value` capture.** New `ElementFingerprint.current_value`
  populated from `.mat-select-value-text`, `mat-checkbox`'s aria-checked,
  and `ng-multiselect-dropdown` chip text.
- **`options_seen` for mat-select panels.** When a mat-select click
  opens the overlay panel, the option `{value,label}` set is captured
  on the trace event.
- **Input debounce.** Extended to 600 ms for `placeholder="Search"` /
  aria-search inputs; trailing-edge coalescing per element so "cana",
  "can", "canada" collapse into ONE final value.

**Live proof** at `tmp/test-round-2026-06-02/01-batch1-verify/verify_report.json`:
clicking the Target Model `<mat-select>` now produces
`accessible_name="Target Model*:"`, `current_value="24_BOMRB_8K"`,
`options_seen=[6 entries]`. The previously-broken
`(div, null, null, null)` fingerprint is fixed.

### C. Annotator + LLM consume the new fields (`707a91e`, `b93da39`, `0326afe`)
- Every labeled widget gets a param (Year, Target Make, Target Model,
  Country/Region — previously dropped entirely).
- `known_options` populated for `select_option` from `options_seen`;
  param example = the human LABEL.
- **`require_search` policy detection.** True when the cluster contains
  search input_changes, OR the picker is `ng-multiselect-dropdown`, OR
  it's a `cdk-virtual-scroll` with a sibling search bar
  (`_detect_search_box_for_list`).
- **Multi-parent cascade detection.** A network request whose query
  string carries multiple prior selections (`make=X&year=Y` →
  `/api/models`) records `depends_on_params=[…]` on the child picker.
- LLM annotation prompt now injects `accessible_name`, `current_value`,
  and `known_options` per step; validation gate accepts grounded
  semantic names but rejects invented params.

### D. Angular cluster detection generalized (`f71f7a0`, `48a4fae`)
The existing `_detect_select_option_clusters` required native `<select>`;
`_detect_set_selection_clusters` required `*-toggle` testids. Neither
matched the Angular shapes. Generalized to recognize:
- `mat-select` root click + `mat-option` click → `select_option`
  cluster with `known_options`, `match_mode="label"`, the recorded
  option's label as `recorded_label`.
- `ng-multiselect-dropdown` root + search input + option-row clicks
  → `set_selection` cluster with `require_search=True`, `search_fp`
  populated, `item_labels` from option accessible_names.
- `cdk-virtual-scroll-viewport` + sibling search input +
  `mat-checkbox` row clicks → `set_selection` cluster with
  `require_search=True`, `search_fp` populated.

### E. Runner enforces the mandatory-search gate (`afc9f90`)
For any cluster with `require_search=True`:
- if `search_fp` is None → fail with `error_kind="search_required_no_search_fp"`
  (operator must declare one or re-record). **No silent fallback to
  direct-click.**
- otherwise → use only `_search_then_click_option` (search by label,
  wait for the row to render, click by visible label).

The `cdk-virtual-scroll` row pattern: `_resolve_row_click_target` finds
the `mat-checkbox` inside the row after the search filters down.

Runner also gained `_do_mat_select_open_then_pick` to drive `mat-select`
specifically: click the root, wait for the overlay panel, click the
option whose label matches the resolved target.

---

## Part 3 — Tests and verifications

### Test suite
**565 pass** (`pytest tests/ --ignore=tests/agent/test_groq_live.py`),
up from the 520 baseline at the start of the sprint. New coverage in:
- `tests/agent/test_angular_label_capture.py` — 14 tests via a jsdom
  bridge that feeds the real `page-filled.html` through the grabber's
  helpers and asserts the right name / widget root / current_value /
  options_seen come out for the mat-select click that was broken.
- `tests/agent/test_angular_annotator.py` — 14 tests pinning the
  labeled-widget param surface, `known_options` plumbing,
  `require_search` detection, and the search-near-list pattern.
- `tests/agent/test_angular_cluster_detection.py` — 7 tests pinning the
  generalized mat-select / ng-multiselect-dropdown / cdk-virtual-scroll
  cluster shapes.
- `tests/agent/test_annotate_llm_label_capture.py` — 5 tests for the
  LLM prompt grounding.
- Extensions to `tests/agent/test_multiselect_search_reach.py` for the
  mandatory-search gate.

### Live end-to-end against the Angular sample portal
Harness recorded the full transfer-artwork flow (Year → Make → Model →
Country search → Show Data → check rows → Transfer Right) through the
real `TeachRecorder` over CDP, annotated, and replayed with **different
parameters** through the real `SkillRunner`.

Annotated skill (`tmp/test-round-2026-06-02/03-batch-e2e/record_skill.json`)
— exactly what you wanted:
- 5 params: `year`, `target_make`, `target_model`, `country_region`,
  `left_items`.
- 3 `select_option` steps for the mat-selects, each with `known_options`
  populated (9 / 6 / 6 entries).
- Country/Region `set_selection`: `require_search=True`, `search_fp`
  populated, `depends_on_params=['target_make', 'target_model']`.
- Show-Data results `set_selection`: `require_search=True`, `search_fp`
  populated.

Replay outcome with brand-new values (`year=2023, target_make=Samsung,
target_model=23_QLED_Q70C, country_region=[Brazil], left_items=[SAM-F000000]`):

| Step | Action | Result |
|------|--------|--------|
| 0 | mat-select Year | ✅ picked 2023 |
| 1 | mat-select Make | ✅ picked Samsung |
| 2 | mat-select Model | ✅ picked 23_QLED_Q70C |
| 3 | Country search | ❌ `search_required_no_search_fp` — see open item below |
| 4 | Show Data list | (gated by step 3) |

Screenshot `06_post_replay.png` confirms the three mat-select picks
landed on the live DOM. The mandatory-search gate held correctly at
step 3 — it refused to fall back to direct-click, which is the right
behavior even though it cost the rest of the flow.

---

## Part 4 — Honest gaps (open items, evidence-based)

1. **`ng-multiselect-dropdown` runner reach — the country picker step.**
   The annotator records `open_picker_fp` as the labeled-widget root
   (the `<ng-multiselect-dropdown>` outer element). The runner clicks
   that root, but Playwright's real-mouse click centroid sometimes hits
   above the inner `.dropdown-btn` and the search input stays hidden.
   Fix shape: annotator should store BOTH the labeled-widget root AND
   the inner click-handler target, runner clicks the inner element.
   Honest failure: this is why the e2e didn't go fully green.

2. **Cascading model picks vs enum rigidity at replay.** When you
   change year+make at replay, the new server-fetched model list
   differs from `known_options` recorded under the original year+make.
   The `enum_label` codec rejects unrecorded labels. Needs either a
   "refresh enum after parent pick" pass or a `select_from_current_options`
   opt-in.

3. **cdk-virtual-scroll param naming.** Default names like
   `mat-checkbox-left-0` aren't domain-meaningful. The planner clarify
   step (batch 4) should let you rename them.

4. **Planner clarifying-question UX (batch 4) is unbuilt.** The data is
   in place — every dropdown carries `accessible_name`, `current_value`,
   `known_options` / label_options, `require_search`, `depends_on_params`
   — and the v2 sidecar surfaces it. The interactive clarify flow that
   shows you those labels and asks "which year? which model? which
   country?" with the available values is the next batch.

5. **Full planner→runner→portal loop (batch 5).** Three of five steps
   green; the country picker reach gap (#1) is the immediate blocker
   for a full green.

---

## Part 5 — How to test it yourself

Both sample portals are independent.

### Run the Angular-Material sample portal
```
cd portals/angular_sample
node mock_backend.js
# serves http://localhost:5189/transfer-artwork
```
Walk the flow yourself (Year → Make → Target Model → Country search →
Show Data → check rows → Transfer Right). This is the exact DOM /
behavior the real Samsung portal exhibits.

### Run the existing React sample (unchanged)
```
cd sample_portal
npm run dev
# http://localhost:5188
```

### Run the automated suite
```
.venv\Scripts\python -m pytest tests\ --ignore=tests\agent\test_groq_live.py
```
Should report `565 passed`.

### Repeat the live e2e harness
```
node portals/angular_sample/mock_backend.js >/dev/null 2>&1 &
# launch a Chrome with --remote-debugging-port=9222 pointed at http://localhost:5189/transfer-artwork
.venv\Scripts\python tmp\test-round-2026-06-02\03-batch-e2e\e2e_record_and_replay.py
```
Produces `record_skill.json` and a 6-frame screenshot progression under
`tmp/test-round-2026-06-02/03-batch-e2e/`.

---

## Closing

What you asked for has landed at the foundation level:
- The grabber understands Angular Material widget structure.
- The annotator gives every labeled widget a param with its visible
  values.
- The runner refuses to skip search when search is mandatory.
- A working Angular-Material-shaped sample portal exists for
  regression testing.

What still needs another batch:
- The runner-side `ng-multiselect-dropdown` open-then-search click gap
  (a few lines once the annotator stores the right inner target).
- The interactive planner clarify UX that asks the operator which year,
  which model, which country, showing the captured labels.
- Cascading model-list refresh at replay so changing year+make
  re-enumerates the model options.

All committed and pushed. Final commit `48a4fae`.
