# 2026-05-28 — Layer B GREEN: id+label multi-select end-to-end

The id+label multi-select design is implemented and the Layer B
end-to-end replay (real TeachRecorder over CDP → annotate → run-skill
through SkillRunner against live Chrome) is **GREEN**. `-p
country=Zimbabwe` ends with exactly a Zimbabwe chip through the real
runner.

## Locked design (implemented)
1. Capture BOTH id and label for EVERY option row seen during a
   multi-select interaction (the whole universe), not just the click.
2. Replay value = the human LABEL (`country=Zimbabwe`); the runner
   resolves label→id internally.
3. Reach an option by its visible LABEL text (search the label, click
   the surfaced row whose text matches) — never via the id template.
4. id+label flows record → annotate → LLM → planner → replay.

## What landed (commits)
- **grabber+schema** (`383ae47`): grabber captures `options_seen`
  (id+label of every rendered row) on multiselect option-click +
  search input; `TraceEvent.options_seen` + `SetSelectionSpec.
  known_options`; teach.py wiring. Native `<select>` snapshot untouched.
- **annotator** (`b4bd243`): `known_options` unioned from all
  `options_seen`; param example/enum in LABELS; **finding 3a** fixed —
  the set_selection primary checkbox click is no longer bound as a
  stray boolean param and the step is relabeled `set_selection_<prefix>`.
- **runner** (`7683bba`): **finding 3b** resolved — label→id via
  `known_options`; reach-by-label-ROW (`_locate_option_row_by_label`,
  never the id template) via `_robust_click`; label-aware equality
  assertion; `set_selection_unknown_label` warn for unseen labels.
- **llm+planner** (`4c21306`): `known_options` surfaced into the
  annotate-LLM context; `SkillParameter.label_options` carries the
  labels to the planner (clarify UX not built, data made available).
- **Layer B fix + artifacts** (this commit): chip-read selector
  excludes the `-remove` sub-button; re-recorded trace, regenerated
  skill, replay artifacts.

## Layer B run (the receipt)
- Re-recorded on **A-1002** (clean draft; A-1001 mutated prior).
  Harness drove: open picker → search "a" (several countries render) →
  search "Argentina" → select. Trace `sessions/b84ec8ed0ce0` carried
  `options_seen` with 45 id+label pairs.
- `annotate --auto`: ONE set_selection step, label
  `set_selection_multiselect_country`, `param_binding: None`, **no stray
  param** (declared params: `country` only), `known_options` = 45 pairs
  (incl. `zw`/`Zimbabwe`), param `example='Argentina'` (label),
  `enum_options` in labels.
- `run-skill … -p country=Zimbabwe --base-url …/asset/A-1002`:
  - step 0 navigate ok
  - step 1 `set_selection(replace, country): +1 -0` **ok** (L1 exact)
  - Live page chip read: `['Zimbabwe×']` — exactly Zimbabwe.
  - Artifacts: `tmp/test-round-2026-05-22/20-layerb/replay-stdout.log`,
    `tmp/test-round-2026-05-22/20-layerb/zimbabwe-chip.png`.

The runner opened the picker, typed "Zimbabwe" into the label-indexed
server search, waited for the surfaced Zimbabwe row, clicked it by label
(NOT the id template `…-checkbox-Zimbabwe`, which would not exist), and
the label-aware equality assertion passed with the `zw` chip.

## One bug Layer B surfaced + fixed
The recorded chip-read selector `[data-testid^='{prefix}-chip-']` also
matched the chip's remove sub-button (`…-chip-zw-remove`), so the read
returned `['zw', 'zw-remove']` and the equality assertion failed on the
first replay even though Zimbabwe was correctly selected. Fixed by
emitting `:not([data-testid$='-remove'])` on the selector.

## Tests
518 pass (was 512 baseline; +6 net new across the sprint). New /
updated coverage: grabber→schema→annotate roundtrip for `options_seen`/
`known_options`; no-stray-param + clean-label regression; param
example/enum in labels; chip-read `-remove` exclusion; runner
reach-by-label-row (id template never clicked); unknown-label warn;
label-aware equality; annotate-LLM context + v2 `label_options`.

## Honest status
Layer B is GREEN end-to-end through the real runner over CDP. The
reach is fully decoupled from the opaque id template (locked decision
#3), so any of the operator's four real-portal pickers (Year / Target
Model / Filter / Country) replays a brand-new label-targeted selection.
The planner clarify UX (offering `label_options` interactively) is data-
ready but not built — intentionally out of scope this sprint.
