# CurationPilot Teach / Annotate / Replay — Senior-Dev Deep Audit Request

## What CurationPilot is

A local supervised browser automation tool. The operator records a
portal workflow once (programming-by-demonstration via Chrome DevTools
Protocol), the trace is converted into a parameterized skill JSON,
and the skill is replayed against the same portal with different
inputs. Single-tenant, runs on the operator's machine.

**Target portals are enterprise OTT / content management portals**
(SPA-heavy, React/Angular, often with auth + cascading dropdowns +
complex multi-select widgets + workflow state machines).

## Code paths the auditor should read deeply

Working tree root:
`D:\PROJECTS\Portal Assistant\Portal-assistant-bot`

Audit these in order. They are all under that root.

**The recording layer:**
- `pilot/overlay/grabber.js` — injected into the portal page via
  CDP. Captures click/input/change/submit/key/file/navigate events
  + emits page_snapshot for the passive catalog + maintains
  quiescence watchers (DOM mutations, in-flight fetch+XHR, request
  log).
- `pilot/teach.py` — Python side that receives events from the
  grabber via `Runtime.addBinding`, writes to `sessions/<id>/
  trace.jsonl`.

**The annotate layer:**
- `pilot/annotate.py` — converts a trace into a parameterized
  Skill JSON. Builds steps, infers param bindings, derives
  fingerprint templates.
- `pilot/agent/annotate_llm.py` — optional LLM enrichment pass
  that proposes semantic param names + v2 sidecar metadata.

**The skill schema:**
- `pilot/skill_models.py` — Skill / SkillStep / SkillParam /
  ElementFingerprint / ExpectedSignals / StepAssertion /
  DisambiguationHint / SetSelectionSpec.

**The replay layer:**
- `pilot/skill_runner.py` — sync Playwright runner with 4-level
  fallback (L1 exact testid → L2 semantic role/name → L3 self-
  heal via LocatorRepair → L4 human takeover). Wait predicates,
  ambiguity detection, set_selection reconciliation, idempotency
  injection, post-condition assertions.
- `pilot/agent/locator_repair.py` — L3 deterministic + optional
  LLM-backed repair.
- `pilot/agent/executor_real.py` — bridges the orchestrator's
  PlanStep onto a SkillRunner invocation. Manages CDP session,
  threading.

**The orchestrator layer:**
- `pilot/agent/orchestrator.py` — preflight → intake → planner
  (clarify-loop) → approve → execute → report. State machine.

**The sample portal (enterprise-shaped test target):**
- `sample_portal/mock_backend.js` — in-memory state machine:
  bearer auth, cascading region→market→language, multi-select
  categories/tags, asset workflow (draft → in_review →
  approved/rejected → published), Idempotency-Key middleware,
  2-3s latencies.
- `sample_portal/src/pages/{Login,Catalog,AssetDetail}.jsx`
- `sample_portal/src/components/MultiSelect.jsx`

## What we want to build (end goal)

A portal automation platform that:

- Records an operator's workflow *with enough structural fidelity*
  that the same workflow replays correctly against DIFFERENT
  parameter inputs (different asset id, different region, different
  category set, etc.).
- Distinguishes structural facts (this click NAVIGATES to a new
  page; this dropdown's options DEPEND on the previous one; this
  field is part of a multi-select set, not a single value) from
  superficial replay (just re-fire the captured clicks).
- At replay, waits only when waiting is actually required, fails
  fast and structurally when assumptions diverge from recording.
- Surfaces clear, actionable errors to the operator (with
  disambiguation modals where the recording's intent was ambiguous,
  retry/skip/abort where it failed cleanly).
- Has a learning loop: operator resolutions of failures persist
  back onto the skill so the next replay doesn't re-fail.

NOT: an autonomous browser agent that decides what to do from
scratch. Recordings are the source of intent.

## The failure pattern we're trying to avoid

A trace shows it concretely: operator clicked "Open A-9001" and
React Router navigated to `/asset/A-9001`. The grabber emitted TWO
events: a click on `btn-open-A-9001` AND a navigate to
`/asset/A-9001`. Annotator turned these into TWO independent steps.
At replay with `input_catalog_search=A-9003`:

1. Step 6 (click): L1 fails (templated testid `btn-open-A-900301`
   doesn't exist due to a bad template), L2 falls back to role+name
   match, finds the only "Open" button → A-9003's row, clicks it,
   React Router navigates to `/asset/A-9003`. ✅
2. Step 7 (navigate): hardcoded literal `/asset/A-9001`, no
   template. Runner calls `page.goto("/asset/A-9001")`. Browser
   navigates AWAY from A-9003 back to A-9001. ✗
3. Steps 8-10: edit + save A-9001 (wrong asset).

The model failure: recording treats click+navigate as two
independent events. Replay can't distinguish "click that caused
navigation" from "click + separate intentional navigation."

I almost shipped a hack-heuristic to drop navigate-after-click
events within 1.5s. The operator stopped me — that would have
broken tab-clicks where the navigation IS intentional and the
operator might want explicit verification.

The user also flagged that even when the navigate URL is templated
correctly to /asset/{search}, calling page.goto() to the same URL
the click already produced is NOT harmless: a goto triggers a full
page reload → React app re-mounts → all the page's API calls fire
again (the catalog APIs, regions, etc.) → the next step's locator
races against those re-fetches. So even "innocuous" duplicate
navigate steps are actually harmful.

## Specific input / interaction types the audit MUST cover

For each, the auditor should consider: what does the grabber
capture, what does the annotator produce, what does the runner do
at replay, where can it break?

1. **Single text input + Enter to submit** — debounced typing →
   multiple `change` events captured as the field is typed; Enter
   key → key event; form submit handler → submit event. At replay,
   re-firing 4 changes then pressing Enter against an empty field
   (replay's start state) means typing the full value four times
   into a now-different field state.

2. **Search box + dropdown of results** — operator types partial
   query, awaits autocomplete, picks one. Recording captures the
   typing, then a click on a result that was rendered AFTER a
   network call. At replay, the network call has different timing
   and may return different results.

3. **Native single select** — `<select>` with `change` event.
   Captured value is the option value. At replay if the option set
   changed (cascading dependency, state-dependent list), exact
   match fails — fuzzy fallback may or may not be right.

4. **Cascading native selects** — Region → Market → Language.
   Picking Region fires `/api/markets?region=…`. The Market
   dropdown's options change. Recording captures Region click +
   Market click but no signal that Market depends on Region. At
   replay with different Region, the recorded Market value doesn't
   exist.

5. **Custom combobox / multi-select** — button to open popover,
   search input inside popover, checkbox per item, chip per
   selection, chip-x to remove. Operator picks N items. Grabber
   captures N opens, N searches, N checks. At replay we want to
   replay against a DIFFERENT set of N items (or different size).

6. **Nested dropdown with multi-select + search** — the picker's
   options themselves are populated by API + filtered by typed
   search + the parent dropdown's value can constrain the set.

7. **Date picker** — native HTML date input (`<input type="date">`)
   AND custom date pickers (calendar widget, day/month/year
   spinbuttons). The recording captures `change` events for native;
   for custom it captures clicks on calendar cells + nav buttons.
   At replay a different target date means different click pattern.

8. **Slider** — `<input type="range">` fires `input` events while
   dragging. Today the grabber only debounces text inputs; range
   isn't covered.

9. **File upload** — `<input type="file">` with operator-picked
   file. Recording captures `file_selected` with filename. At
   replay we want to upload a DIFFERENT file with the same shape.

10. **Drag and drop** — entirely uncovered by today's grabber.

11. **Accordion / collapse panels** — clicking the header toggles
    visibility. Captured as a normal click. At replay the
    accordion's current state may differ from recording (already
    expanded vs collapsed).

12. **Modal open / close** — clicking an action button opens a
    modal; clicking outside or Esc closes it. Recording captures
    the open click. The close may be a click on the page root
    (captured as click on `root` div) or an Esc keypress. At
    replay these are fragile.

13. **Click that navigates (route change)** — the bug case above.

14. **Click that opens a popup / new window** — entirely uncovered.

15. **Auth-required workflows** — pre-flight + auth_signal exists;
    sign-in workflow being recorded is a separate problem.

16. **Tables with virtualized / paginated rows** — recording
    captures a click on row N (specific testid). At replay with a
    larger dataset, row N may not exist or may be at a different
    scroll position.

17. **Scroll** — operator scrolls to bring an element into view.
    The grabber filters scroll events deliberately. At replay
    Playwright auto-scrolls into view for most actions, but for
    portals that hydrate on scroll (infinite scroll, virtual
    scroll), recording's "I saw row N" may need explicit scroll.

## Already considered patterns I REJECTED

- **Drop a navigate event that follows a click within 1.5s.**
  Rejected: would break tab-clicks where the navigate IS the
  intended action.
- **Make the runner skip duplicate navigates by URL match.**
  Rejected: not all duplicates are safe (a re-navigate to the
  same URL triggers a full reload, which fires all the page's
  initial API calls; the next step then races those).
- **Just template the navigate URL based on substring matching.**
  Tentative: solves the literal bug in the failure pattern above
  but doesn't fix the structural confusion between
  click-with-navigation and standalone-navigate.

## Already considered patterns I LIKE (but want pushback on)

- **Correlate at the grabber.** On every click, set a watcher
  for `history.pushState` / `popstate` for ~500ms. If fired,
  attach `caused_navigation_to: <url>` to the click event AND
  suppress the standalone navigate event. Standalone navigates
  remain rare and intentional (manual URL bar entry).
- **Schema additions for proper modeling:** `SkillStep` gains
  `caused_navigation_to_template: str | None` and `expects_url_after`
  field. Click steps with those carry their own URL-change
  verification.
- **For cascading dropdowns:** declare `depends_on` on params
  (already in schema) but ALSO record the network call that
  refreshes the dependent's options (we record it; we don't
  surface it on annotate-time).

## Specific questions for the auditor

1. **Dry-run each of the 17 input/interaction types above.** For
   each: what does today's grabber capture, what does today's
   annotator produce, where does the runner break, and what's the
   minimum fix?

2. **Find every place in `pilot/skill_runner.py` and
   `pilot/annotate.py` where a hardcoded heuristic could break
   the unexpected case.** Examples I'm worried about:
   - `_select_option_with_fuzzy_fallback` threshold of 0.7
   - `_wait_for_page_settle` magic numbers (250ms quiet, 2000ms
     dom timeout, 4000ms total ceiling)
   - `_detect_ambiguity` only fires for templated fingerprints
   - The 400ms debounce in `grabber.js` for text input
   - The `_SPINNER_SELECTOR` testid prefix list (`status-saving`,
     `loading-`, `spinner-`, etc.) — only catches portals using
     these conventions
   - The 60s default timeout on the Groq client
   - The 1.5s gap I almost used for navigate-drop
   - The substring-based template detection (requires len >= 3,
     uses replace() which catches the first occurrence)
   - The `_first_visible` helper that catches all Exceptions then
     falls back to `count() > 0` — masks real failures
   - The `_css_escape` that only escapes `\` and `'`
   - The `Idempotency-Key` strips only query+hash; what about
     headers, form-encoded bodies, etc.

3. **For each hardcoded threshold/heuristic you find, propose:
   (a) what real-world portal would break it, (b) a more robust
   alternative.**

4. **Identify "wrong layer" assumptions.** Where is the recorder
   doing something the annotator should do? Where is the annotator
   making a decision that should live on the skill schema as
   data? Where is the runner re-deriving something the annotator
   should have produced?

5. **Identify race conditions.** Especially: anywhere a wait
   predicate could falsely satisfy (return true while the actual
   state isn't ready). The `__cp_opt_seen` global state, the
   `__cp_request_log` ring buffer, the in-flight count.

6. **Identify silent failures.** Empty `except Exception: pass`
   sites that should at least log. The user-facing surface (the
   Replay UI) only shows things the orchestrator emits as events;
   silent runner failures don't surface anywhere visible.

7. **For the specific bug case (click + navigate-after for SPA
   route change), give your recommended end-state design.**
   Include schema changes, grabber changes, annotator changes,
   runner changes. Be specific about what the Skill JSON should
   look like for a click-that-navigates step. Compare to what
   it looks like today.

8. **For the cascading-dropdown case (Region → Market → Language),
   give your recommended end-state design.** Today the schema has
   `depends_on` and `expected_signals` but the annotator doesn't
   populate them. What should it do?

9. **For the multi-select case (Categories + Tags), give your
   recommended end-state design.** Today the schema has
   `set_selection` but the annotator doesn't produce these — they
   must be hand-curated. What should the annotator detect?

10. **What did I miss?** The operator listed 17 interaction types
    + the navigate-on-same-URL re-fetch race. What other
    interaction types or failure patterns from real enterprise
    portals (OTT, content management, asset workflow, dashboards
    with filters, multi-tenant admin UIs) should we be modeling?

## Output format

I want a **senior-developer-grade** audit report. Not generic
advice. Concrete file:line citations. Concrete proposed code
shapes. Honest opinions on what to fix first.

Order findings as:
- **BLOCKERS** — things that will break common real-portal flows
  right now
- **MAJOR** — fragile patterns that will break under realistic
  variation
- **MINOR** — code-quality concerns
- **STRATEGIC** — model / architecture issues that need bigger
  thinking

Skip nitpicks. Skip generic "consider adding tests" advice (we'll
add tests after the structural fixes). Push back hard on
patterns that look right but aren't.

Word budget: please be thorough; this is a real architectural
audit. ~3000-4000 words is reasonable. Be specific.
