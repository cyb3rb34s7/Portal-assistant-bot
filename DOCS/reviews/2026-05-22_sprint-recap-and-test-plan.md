# 2026-05-22 — Structural fix sprint recap + browser test round plan

## What was done

Branch `feat/enterprise-portal-and-additive-sprint` at `36c5d64`, pushed.
Tests: **502/502 pass**.

Sixty work items shipped in 60 commits:

- **Foundation drifts** (F-01..F-10) — fixes for bugs I introduced in
  my own work on WI-01..WI-04 / WI-07, surfaced by an independent
  GPT 5.5 audit. Highlights: typed `OptionSnapshot` (silent fingerprint
  drop fix), sample portal retry safety (runner key actually wins),
  XHR header-name correctness, fetch Request-object handling,
  causality graph runs ahead of `filter_events`, page_state populated,
  request/DOM observations emitted as TraceEvents, six new hardcoded
  heuristics moved to `WaitPolicy`/`IdempotencyCapability` config,
  five schema validators added, one pre-existing test signature fixed.

- **Schema foundation** (WI-01..WI-07) — extended ActionType for 14
  structured action types, StepEffect family (navigation / popup /
  download / modal / toast / new-tab / state-change), ReplayPolicy
  with fail-fast default, ParamProvenance + StepProvenance, structured
  TraceEvent with causality (event_id, interaction_id, caused_by,
  sequence, monotonic_ts), richer ElementFingerprint (28 control_kind
  values, options_snapshot, ARIA state, locale/timezone hints,
  min/max/step), `PortalContext.idempotency` as capability data
  (no more global injection), typed params + codecs (boolean / enum /
  number_range / iso_date / localized_number / file_ref / etc),
  silent failures converted to structured Diagnostic events,
  on_failure="abort" default with explicit opt-out.

- **Annotator core** (WI-08..WI-14) — click+caused-navigate folds
  into one step with `effects.navigation` (no more rogue page.goto),
  action-scoped network/DOM baselines (no stale-request matching),
  observed readiness signals replace spinner-by-convention,
  provenance-based templates replace substring luck (`btn-open-A-9001`
  no longer templates as `{search}01`), semantic clustering pipeline,
  clear-field actions preserved as real value transitions, click
  gesture/effect classification (double/repeat/toggle/open/close).

- **Form widget semantics** (WI-15..WI-21) — fill_submit collapses
  text bursts + Enter, select_autocomplete separates query from
  selected_item, select_option requires declared aliases (no fuzzy
  "US -> UAE"), cascading select dependency model, set_selection
  reconciles multi-select to a target set with `final_equality_assertion`,
  nested/searchable multi-select with depends_on, date_select for
  native + custom calendar widgets.

- **Locator resolution + repair safety** (WI-22..WI-27) — ambiguity
  detection at every locator level (L1/L2/alternates/repaired),
  LocatorProbeResult replaces _first_visible exception masking, safe
  selector construction (no more `_css_escape` gaps), declared aliases
  + structural failure, RepairPolicy gates L3 acceptance on uniqueness
  not just score, action-specific postconditions replace whole-page
  signature for heal verification.

- **Additional interactions** (WI-28..WI-38) — sliders (collapsed
  drag burst), file uploads with accept-attribute validation,
  drag/drop via Playwright.drag_to, LLM annotate enriches structure
  via validated overlays, iframe + shadow DOM traversal, accordion
  as desired-state toggle, modal/dialog effects with close mechanism
  classification, popup/new-window workflows with multi-page session,
  auth preconditions, virtualized table scroll_until, signal-driven
  scroll (no magic counts).

- **Missed coverage** (WI-39..WI-49) — contenteditable rich text,
  hover-driven menus, global keyboard shortcuts (Ctrl+S), toast-driven
  undo/conflict, disabled→enabled readiness as first-class wait,
  server validation errors as classifiable signals, download/export
  workflows via expect_download, cross-tab via PageContext routing,
  WebSocket/SSE push as first-class expected_signals,
  locale/timezone normalization through codecs + strict_locale gate,
  canvas/SVG/media adapter registry with noop_click sample.

- **Verification + housekeeping** (WI-50 + follow-ups) — regression
  matrix (one test per WI), skill upgrader (legacy v1 → v2 with
  back-compat policy), engineering guide (`STRUCTURAL_FIX_GUIDE.md`),
  completion log, agent-browser smoke (single happy-path scenario),
  Pydantic `extra="forbid"` on six audit-critical models, five
  remaining grabber heuristics surfaced through `WaitPolicy`,
  honest accounting on L3 score bands (16/17 audit rows fully CLOSED,
  1 PARTIALLY CLOSED — the bands still gate `confidence == "low"`
  refusal at 0.55, with WI-26 RepairPolicy as the structural gate
  above that floor).

## What was NOT verified end-to-end

Out of 60 work items, only ONE happy-path scenario got exercised via
agent-browser (catalog → login → search → open → edit title → save).
The 502 unit tests verify Python schemas, annotator detectors, runner
dispatch, and diagnostic emission — but NOT the CDP+Playwright
integration for new widget paths.

Specifically untested via a real browser:

- WI-18 cascading Region → Market → Language
- WI-19 / WI-20 multi-select reconciliation (Categories, Tags)
- WI-21 date picker
- WI-28 priority slider
- WI-29 file upload validation
- WI-30 drag/drop between zones
- WI-33 accordion toggle
- WI-34 modal confirmation flows
- WI-35 / WI-46 popup / cross-tab
- WI-37 / WI-38 scroll_until on virtualized lists
- WI-39 contenteditable rich text
- WI-40 hover menus
- WI-41 Ctrl+S shortcut
- WI-42 toast detection
- WI-43 disabled→enabled readiness
- WI-44 server validation errors
- WI-45 download capture
- WI-47 push frames
- WI-48 locale mismatch
- Destructive workflow chain: submit_review → approve → publish
- The runner itself driving any of these via CDP

A bug at the integration layer (wrong locator API, wrong event
dispatch order, wrong wait semantics against real DOM) would not
be caught by what landed.

## Test round plan (this session)

Two layers, ordered by ROI:

### Layer A — sample portal regression matrix via agent-browser

Drive each high-stakes scenario directly in the sample portal with
the `agent-browser` CLI. Verifies the sample portal itself still
supports the interactions the runner expects (grabber/script changes
in the sprint touched the portal's pages). Pass = the interaction
behaves as designed.

Scenarios (priority order):

1. **Login + catalog** — basic regression sanity
2. **Asset detail edit** — title, comment, publish_at
3. **Cascading dropdowns** — Region (APAC) → Market (IN) → Language (hi-IN)
4. **Multi-select** — Categories: add Sports + Drama, remove Drama
5. **Multi-select with search** — Tags: search "kids", check matches
6. **Priority slider** — set to 75
7. **Accordion** — open advanced settings panel
8. **Drag and drop** — move promo-1 from Included → Excluded
9. **Date picker** — set publish_at
10. **Contenteditable** — type rich text into description
11. **Mega-menu hover + click** — File > Save As
12. **Ctrl+S shortcut** — global save
13. **Save → success toast** — observe toast detection
14. **Save with empty title → validation error** — observe inline error
15. **File upload** — pick a CSV
16. **Destructive chain** — submit_review → approve → publish
17. **Search autocomplete on catalog** — type "A-9" → results render
18. **Scroll-to-find on catalog** — paginated/scrolled row find

Each scenario captures a transcript at `tmp/test-round-2026-05-22/<NN>-<scenario>/`
with snapshots + screenshots. Any portal regression triggers a fix
in a follow-up commit; documentation updates roll in.

### Layer B — runner end-to-end via teach + replay (deferred)

Recording one skill per high-stakes WI via `python -m pilot teach`
against the sample portal, then replaying via the runner. This is
the true contract test for the runner's CDP+Playwright integration.
Layer A first surfaces sample-portal issues; Layer B requires a
clean Layer A.

If Layer A reveals significant portal regressions, fix those first;
postpone Layer B accordingly.

## What "iterate" means here

For each scenario:
- Run via agent-browser; capture artifacts
- If something behaves wrong (portal regression), open the affected
  source, fix, recommit, re-run
- If something behaves as designed, mark scenario green and proceed
- After Layer A completes, write a brief layer-A report at
  `DOCS/reviews/2026-05-22_test-round-layer-a.md`

Failures + fixes get separate commits. No commits to `main`.
