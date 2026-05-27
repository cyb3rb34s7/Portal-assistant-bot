# 2026-05-22 — Multi-select-with-search fix + browser test round

Status report for review while away. Branch
`feat/enterprise-portal-and-additive-sprint`, pushed. **508 tests pass.**

---

## 1. The real-portal problem you reported

On the real OTT "Migrate framework" page, the multi-select dropdowns
(Year, Target Model, Filter, Country — all "click to open → search box →
checkboxes → chips") failed at replay two ways:

- **Case A — scroll-to-find:** operator recorded clicking an option that
  was visible; at replay we want a *different* option that's below the
  fold → the runner couldn't reach it.
- **Case B — search-to-find:** operator typed into the search box then
  clicked the result → replay couldn't reproduce that for a different
  target.

## 2. Root cause (both real bugs, now fixed)

- **Case B:** the runner filled the picker's search box with the option's
  **id** (`"zw"`) instead of its **label** (`"Zimbabwe"`). The portal's
  search is label-indexed, so the id surfaced nothing → option never
  found. (`pilot/skill_runner.py`, old `_do_set_selection`.)
- **Case A:** the runner built a locator and clicked but never scrolled
  the option into view and never fell back to search → an off-viewport
  target was unreachable.

## 3. Design decision (you approved these)

Reaching an option is **replay-time logic, not replayed keystrokes**. The
structural fact is *"the picker's selected set == target list"*; how each
option is reached is incidental. Locked decisions:

- **Always prefer search-to-narrow** when the picker has a search box,
  regardless of how the operator originally reached the option.
- **Lists are non-virtualized** ("all there, just long") → no
  scroll-until-render machinery; locator + `scroll_into_view_if_needed`
  reaches any rendered option as the fallback.
- **Search is server-side (spinner)** → replay waits for the *target
  option's checkbox to become visible* after typing the label. That
  visibility wait rides out the server fetch with **no fixed sleep**;
  structured failure if it never surfaces.

## 4. What was implemented

Commits (all pushed):

| Commit | Change |
|--------|--------|
| `5fa6db6` | **Schema + runner.** `SetSelectionSpec` gains `item_labels` (id→label), `select_strategy` (`search`/`scroll`/`direct`), `option_list_selector`. `_do_set_selection` refactored into `_reach_and_click_option` + `_search_then_click_option`: search fills the LABEL, waits for the target checkbox to be visible (bounded by `wait_policy`), clicks, clears search; direct path scrolls into view then clicks; direct-not-found falls back to search when a search box exists. |
| `7ff0ada` | **Annotator.** `_build_set_selection_spec` captures `item_labels` from each option's `accessible_name`→`text`→`aria_label`, and sets `select_strategy="search"` when the picker has a search box, else `"direct"`. Derives `option_list_selector` from the picker prefix. |
| `a689283` | **Sample portal repro fixture.** A server-backed **Country** multi-select (~54 alphabetical options, Zimbabwe last/below-fold) with `/api/countries?q=` search + artificial spinner delay. `MultiSelect` gains an optional `fetchOptions` prop so existing client-side pickers are untouched. |
| `<this push>` | **Tests + browser verification artifacts.** 6 unit tests + agent-browser transcripts/screenshots. |

All new fields default to legacy behavior — existing skills are unchanged.

## 5. Verification

### Unit tests (6 new, 508 total) — `tests/agent/test_multiselect_search_reach.py`
- Schema roundtrip for the three new fields + legacy defaults preserved.
- Annotator sets `search` strategy + captures labels when a search box is
  present (Case B); falls back to `direct` with labels still captured
  when not (Case A).
- **Runner search path types the LABEL not the id**, gates the click on
  the checkbox becoming visible, clears search between items.
- Runner direct path calls `scroll_into_view_if_needed` before click.

### Live browser (agent-browser against the sample portal Country picker)
Artifacts: `tmp/test-round-2026-05-22/19-country-multiselect/`
- **Search path:** open picker → type "zim" → server search settles →
  list narrows to exactly `["Zimbabwe"]` → click → "Zimbabwe" chip
  appears. (Proves Case B's interaction + the server-side settle.)
- **Scroll path:** full 54-option list restored, last option confirmed
  **below the fold** → `scrollIntoView` makes "United States" visible →
  click → chip appears. Final chips: "Zimbabwe, United States" — both
  reached, neither was in the initial viewport. (Proves Case A.)

## 6. Honest verification boundary

- **Verified by unit test:** the schema contract, the annotator's
  strategy/label derivation, and the runner's reach logic (label-not-id,
  visibility-gating, scroll-before-click, search fallback).
- **Verified by live browser:** the sample portal *supports* both reach
  interactions, including the server-side search spinner.
- **NOT yet verified end-to-end:** the full
  teach → annotate → replay loop driven by the runner against a live
  Chrome over CDP for this picker. The runner code paths are unit-tested
  with a mocked page; a true contract test (record a Country selection
  via `python -m pilot teach`, replay it with a different target via the
  runner) is the remaining gap. This is "Layer B" in the test plan and
  needs the CurationPilot server + a teach session, which I did not spin
  up this round.

## 7. Other observations from the test round

- **agent-browser/CDP click quirk (not a portal bug):** agent-browser's
  `click` does not reliably trigger this React app's button `onClick`
  handlers (login Sign-in, catalog Search, multiselect toggle). The
  handlers DO fire via form `requestSubmit()` or a direct `.click()` in
  `eval`, and via real user clicks. I drove the verification with `eval`
  where needed. This affects how I *test*, not the portal's or the
  runner's correctness — the runner uses Playwright (not agent-browser),
  which does drive React handlers correctly.
- **Cascading dropdowns (Region→Market→Language)** verified working live
  earlier in the round: APAC → India → Hindi cascaded with the right
  server-fetched option sets. (Artifacts under
  `tmp/test-round-2026-05-22/03-cascading/`.)
- **Categories multiselect** (client-side) verified: open → check Sports
  + Drama → chips "Sports, Drama" → close.

## 8. Remaining test-round scenarios (not yet run)

From the 18-scenario Layer A matrix in
`2026-05-22_sprint-recap-and-test-plan.md`, still pending live drive:
date picker, priority slider, accordion, drag/drop, contenteditable,
hover mega-menu, Ctrl+S shortcut, toast on save, validation error on
empty title, file upload, the destructive workflow chain
(submit_review → approve → publish), and the catalog search/scroll. Plus
the Layer B runner end-to-end loop noted in §6. These are the next
session's work.

## 9. Bottom line

Your two reported failures (multi-select search-by-id and off-viewport
unreachable) are **fixed and verified** at the unit + portal-interaction
level. The fix generalizes to all four of your real-portal pickers (Year,
Target Model, Filter, Country) because they share the same
toggle/search/checkbox/chip structure. The one honest gap is the full
record→replay loop driven through the runner against live Chrome, which
needs a teach session to close.
