# id+label multi-select design — Layer B end-to-end green

## Context
A teach→annotate→replay pipeline records operator demos of a portal task
and replays them. The hard case: a custom multi-select picker with a
SERVER-SIDE, LABEL-indexed search. `/api/countries?q=Zimbabwe` → `[{id:zw,
name:Zimbabwe}]`; `q=zw` → `[]`. Option DOM testid is id-indexed:
`multiselect-country-checkbox-<id>` (e.g. `-checkbox-zw`); the visible
label is the option's accessible name ("Zimbabwe").

The recorder captures one option click (Argentina, id=ar). The annotator
produces a `set_selection` step with `item_labels={"ar":"Argentina"}`,
`select_strategy="search"`, a `checkbox_template_fp` (testid
`multiselect-country-checkbox-{item}`). The runner diffs current vs target,
searches per item, clicks.

Two open bugs block replay of a NEW target (`-p country=Zimbabwe`):
- **3a stray param:** the checkbox-click event (the cluster's primary
  target) ALSO gets bound as its own boolean param
  `multiselect_country_checkbox_ar`, so the step declares TWO params and
  replay aborts "needs parameter multiselect_country_checkbox_ar".
- **3b id-vs-label:** replay value "Zimbabwe" is treated as the ID. The
  search types "Zimbabwe" (works), but the click uses the id-template →
  `multiselect-country-checkbox-Zimbabwe` which doesn't exist (real testid
  is `-checkbox-zw`). Reach fails.

## Locked decisions (operator-approved, not up for debate)
1. Capture BOTH id and label for EVERY option that renders during a
   multi-select interaction (the whole universe seen), not just the click.
2. Replay value = the human LABEL (`country=Zimbabwe`). Runner resolves
   label→id internally.
3. Reach an option by its visible LABEL text (search the label, click the
   surfaced row whose text matches) — never depend on the id template for
   the click. The id is still captured (for LLM, planner, aliases).
4. id+label flows record→annotate→LLM→planner→replay.

## Proposed approach (in execution order, 5 commits)

### A. Grabber (`pilot/overlay/grabber.js`)
Add an `options_seen` field (list of `{value,label}`) to the click AND
input_change event payloads, populated ONLY for multiselect interactions.
At a multiselect option click and at a multiselect-search input_change,
scan the picker's popover for every rendered option row
(`[data-testid*='-checkbox-']` under the same picker prefix), extract
id (from testid suffix) + label (accessible name / row text). This rides
server-search results because the operator types, results render, THEN we
capture on the next interaction. Don't touch the native-select
`options_snapshot` path. Keep testid conventions.

### B. Schema
- TraceEvent: add `options_seen: Optional[list[OptionSnapshot]] = None`.
  Wire through teach.py.
- SetSelectionSpec: add `known_options: list[OptionSnapshot]` (reuse
  OptionSnapshot; value=id, label=label). Superset of item_labels.

### C. Annotator
- Populate `known_options` by unioning every `options_seen` across the
  cluster's events (dedup by id, first-label-wins).
- Set the list param's `example` + `enum_options` to LABELS.
- Fix 3a: when a step's action is `set_selection` (cluster-driven),
  suppress the step's own `param_binding` and exclude its
  primary-target event from the param-binding declaration pass, so the
  step declares ONLY the list param.

### D. Runner
- Resolve each target LABEL → id via `known_options` (label→value,
  case-insensitive) for the equality assertion + diagnostics. The diff
  set stays id-keyed when resolvable; falls back to the label itself as
  the "id" when not in known_options.
- Reach by label: type the label into search, wait (visibility-gated) for
  an option ROW whose visible text matches the label, click THAT row via
  `_robust_click`. New row-by-label locator: scope to popover, match a
  row whose text == label (case-insensitive). Do NOT use the id template
  for the click.
- Equality assertion label-aware: read chips, compare by label
  (case-insensitive) to target labels; map chip ids→labels via
  known_options. The recorded id-path stays as fallback.
- Unknown label (not in known_options): still attempt search-by-label
  reach; emit `runner.set_selection_unknown_label` (warn, not fatal).

### E. LLM + planner data plumbing
- annotate_llm.py: include known_options (id+label) in the LLM context
  for set_selection params. Keep validation gate.
- Ensure the v2 param carries the label list so a future planner clarify
  step can offer labels. Don't build the clarify UX; just make data
  available + note where planner consumes it.

## Already considered and rejected
- New TraceEvent `kind="options_seen"`: rejected — forces a new Literal
  member + teach + annotate + every-consumer change, more blast radius
  than a field on existing events.
- Keeping replay value as id with a separate label param: rejected —
  violates locked decision #2 (operator types labels).
- Clicking via id template after resolving label→id from known_options:
  rejected — a NEW target not seen at record has no id in known_options;
  locked decision #3 says reach must never depend on the id template.

## Specific questions
1. Reaching the row by label text: is matching the row whose visible text
   equals the label (case-insensitive, scoped to the popover) robust
   enough, or should I prefer matching the checkbox's accessible name /
   the row's label element specifically to avoid matching a substring row
   (e.g. "Argentina" vs "Argentine Republic")? I lean exact-text-match on
   the row's label node, with the visibility wait as the gate.
2. The diff is id-keyed. When a target label resolves to an id via
   known_options, the diff/assertion use the id. When it does NOT (unknown
   label), I use the label string as the pseudo-id for diffing. Is mixing
   real-ids and label-pseudo-ids in one set a correctness hazard for the
   equality assertion, or acceptable given the assertion is label-aware?
3. Fix 3a: suppressing the binding for set_selection primary-target
   events — any case where the same event legitimately needs its own
   param? (I believe no: the list param fully describes the selection.)
4. options_seen capture at click + search-input time: am I missing option
   rows that render and disappear BETWEEN those two events (e.g. operator
   searches "a", sees 10, searches "arg", sees 1, clicks)? Should I also
   hook a MutationObserver on the popover to catch transient renders, or
   is capture-at-each-interaction sufficient for the known_options goal?
