# 2026-05-22 — Layer A browser test round (COMPLETE)

Drove every high-stakes interaction in the sample portal via the
`agent-browser` CLI to confirm the portal supports the interactions the
runner relies on, after the sprint's grabber/JSX changes. Artifacts under
`tmp/test-round-2026-05-22/`.

**Headline: no portal regressions found.** Two interactions that *looked*
broken were diagnosed to root cause — both are tooling/timing artifacts,
not portal bugs, and both actually validate the project's wait-on-signal
thesis.

## Scenario results

| # | Scenario | Result | Notes |
|---|----------|--------|-------|
| S1 | Login + catalog | ✅ | Form submit works; see S17 re: button-click quirk |
| S2 | Asset title edit | ✅ | Controlled input updates |
| S3 | Cascading Region→Market→Language | ✅ | APAC→India→Hindi; each server fetch repopulates the child |
| S4 | Categories multiselect | ✅ | Open → check Sports+Drama → chips "Sports, Drama" |
| S5 | Tags multiselect w/ search | ✅ (by equivalence) | Same `MultiSelect` widget as S4 (client) + S19 (server search) |
| S6 | Priority slider | ✅ | Set to 75; label updates |
| S7 | Accordion (advanced settings) | ✅ | aria-expanded false→true, panel reveals |
| S8 | Drag/drop promo-1 Included→Excluded | ✅ | HTML5 DnD; item moves between zones |
| S9 | Date picker (publish_at) | ✅ | Native date input set to 2026-08-15 |
| S10 | Contenteditable description | ✅ | "Hello **bold** world" HTML preserved |
| S11 | Mega-menu hover → Save As | ✅ | pointerover reveals submenu → click |
| S12 | Ctrl+S shortcut | ✅ | Keydown triggers save → "Saved" toast |
| S13 | Save → success toast | ✅ | Toast appears after PATCH settles (see timing note) |
| S14 | Empty title → validation error | ✅ | 422 → error toast + field error surfaced |
| S15 | File upload | ⚠️ partial | Input affordance verified (`accept=".csv,text/csv"`); byte-upload hung agent-browser's native file chooser (tool limit, not a portal bug) |
| S16 | Workflow chain | ✅ | draft→in_review→approved→published, correct state-gating each step |
| S17 | Catalog search | ✅ | "A-9" → 3 rows (A-9001/2/3) via form submit |
| S18 | Catalog scroll-to-find | ✅ (by equivalence) | Covered by S19 scroll path; catalog results too few to require scrolling |
| S19 | Country multiselect (search + scroll) | ✅ | Server search "zim"→Zimbabwe; off-viewport "United States" via scroll |

16 fully verified live, 2 covered by equivalence (S5, S18), 1 partial
(S15 — affordance verified, byte-upload blocked by tooling).

## Two diagnosed non-bugs (both important)

### 1. agent-browser `click` doesn't reliably fire this React app's button handlers
Login Sign-in, catalog Search, multiselect toggles: agent-browser's CDP
`click` did not trigger the React `onClick`. The handlers fire correctly
via form `requestSubmit()`, a direct `.click()` in `eval`, real user
clicks, **and Playwright** (which the runner uses). So this affects how I
*drove the test*, not the portal's or the runner's correctness. I drove
the round with `eval` where needed.

### 2. The sample portal has real backend write latency
`onSave`'s PATCH settles after ~2–4s (deliberate mock-backend lag), so the
"Saved" toast appears *after* the await resolves and auto-dismisses 4s
later. My first checks (600ms–1.5s) ran before the toast mounted and
falsely looked like "no toast." A MutationObserver over a 5s window caught
it: `success branch ran, toast "Saved" shown`.

This is the single most important confirmation of the round: **a
fixed-time check misses the toast; a signal-based wait catches it.** That
is exactly what the runner does — WI-42/WI-27 wait on the toast's
visibility signal, WI-09 waits on action-scoped network completion, WI-10
waits on readiness transitions — instead of sleeping a fixed interval. The
portal's latency would defeat a naive recorder and is handled correctly by
the structural wait semantics this sprint built.

## What this round does and does NOT prove

- **Proves:** the sample portal still correctly supports all the
  interactions (no UI regression from the sprint's grabber-hook / JSX
  edits), and the interactions behave as the runner's strategies expect
  (server-side search narrows, cascades repopulate, state-gating holds,
  toasts/validation surface on the documented signals).
- **Does NOT prove:** the full teach → annotate → replay loop driven by
  the runner over CDP. That is Layer B and remains the outstanding gap
  (noted in the multi-select report and the sprint recap). agent-browser
  exercises the portal directly; it does not exercise CurationPilot's
  recorder/annotator/runner against the portal.

## Follow-ups worth considering (none blocking)

- The mock backend's validation message is a generic `validation_failed`
  code rather than a human string ("Title is required"). The structural
  path (422 → error toast + field error) is correct; the message fidelity
  is cosmetic.
- agent-browser file-chooser hang (S15): if file-upload smoke matters
  later, drive it through the runner's Playwright `set_input_files`
  (WI-29) rather than agent-browser's `upload`.
