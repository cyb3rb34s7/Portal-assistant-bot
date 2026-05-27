# CurationPilot — Additive Sprint Plan (Second-Opinion Review)

## What CurationPilot is

A local supervised browser automation tool for enterprise OTT
content-curation portals. Operator records a workflow once
(programming-by-demonstration via CDP); system extracts a
parameterized "skill" (JSON); planner picks a skill from a natural-
language goal + attachments; runner replays via CDP with a 4-level
locator fallback (L1 exact testid → L2 semantic role/name → L3
self-heal via deterministic + LLM-backed `LocatorRepair` → L4 human
takeover). Single-tenant, runs on the operator's machine.

Current stack:
- Python runner: `pilot/skill_runner.py` (sync Playwright)
- Async orchestrator: `pilot/agent/orchestrator.py` (intake →
  clarify-loop → plan → approve → execute → report)
- FastAPI + WebSocket bus: `pilot/agent/web_server.py`
- React UI: `curationpilot-app/` (Vite + Zustand)
- Sample portal: `sample_portal/` (Vite + React, mock backend)
- LLM: Groq for intake / planner / reporter / annotate-LLM;
  deterministic-only fallback path exists.

Skills are JSON files with templated fingerprints (testid
`row-{content_id}` substitutes the param value at replay).

## What's broken today (operator-validated)

1. **Recording is "sole truth"** — captures the operator's exact
   clicks; replay just re-fires those clicks with templated values.
2. **Fallbacks invisible in practice** — operator reports never
   seeing L2/L3 trigger; failures jump straight to abort/retry.
   (Almost certainly an observability problem, not a fallback
   problem — but unconfirmed.)
3. **Multi-select dropdowns** — recording captures N specific
   checkbox clicks; replay can't grow/shrink the selection.
4. **Cascading dropdowns** — Country → State. At replay with a
   different Country, the recorded State value doesn't exist.
5. **Network lag** — `_wait_for_page_settle` polls
   `window.__cp_inflight === 0` + DOM quiescence + spinner-by-
   convention. Works for clean SaaS but breaks on portals with
   long-poll, SSE, or constant `/api/notifications` polling
   (in-flight is never 0).
6. **Replay assumes Chrome+CDP is up.** No pre-flight check, no
   auth probe, no 2FA gate. If the operator closes Chrome between
   sessions, replay crashes mid-step.
7. **No learning loop.** When an operator resolves an
   `ambiguous_target` by picking a row, that pick is one-off; the
   next replay against the same skill makes the same mistake.

## The decision we made before this review

A peer agent proposed a full rewrite: chunk traces into atomic
composable skills (`navigate_to_curation`, `select_layout`,
`fill_slot`, etc.) with entry/exit assertions, then have the
planner compose plans across skills. I pushed back: that's 4-6
weeks of rewrite with no real-portal value during the rewrite, and
all of the operator's stated pains can be solved as **additive
metadata on the existing skill JSON** without a chunker, without
cross-skill composition, and without breaking existing skills.

Composition is the right *end state* but only after we've measured
which problems remain after the additive sprint. If 80% of failures
after the sprint are "wrong skill picked / wrong sub-flow," then
composition has earned its place. If they're "vision misread the
screen" or "portal redesigned," composition wouldn't have helped.

## The additive sprint — ~2 weeks of work, all on the current schema

### 1. Pre-flight + 2FA gate (day 1)

New orchestrator phase before intake:
- CDP doctor probe — Chrome reachable?
- Tab match — tab on portal `base_url` exists?
- Auth probe — visit `base_url`, look for a configurable
  `auth_signal` (e.g. presence of `[data-testid='nav-user-menu']`
  or absence of a login form). If absent, emit `paused { reason:
  "auth_required" }` and wait for operator to resolve.
- Mid-run auth handling — same probe runs after every `navigate`;
  if auth disappears mid-run, pause again.

Schema: add `auth_signal` to `portals/<id>/context.yaml`.

### 2. Observability sweep (day 1)

Surface fallback-level info that's already captured in audit logs.
Add a "Run Diagnostics" panel to the Sessions tab in the UI that
shows: per-step L-level distribution, heal count, self-heal
confidence histogram, time-in-wait per step. Without this, the
"L2/L3 never fire" claim is unverifiable.

### 3. `expected_signals` per step (days 2-3)

Recording side: grabber already hooks `window.fetch` +
`XMLHttpRequest.prototype.send`. Add a "snapshot network activity
in a ~2s window after each captured action" pass that emits a
per-action `network_observed` event with URL patterns + durations.

Skill JSON gains:
```json
{
  "expected_signals": {
    "network": [{"url_pattern": "/api/states*", "max_ms": 1000}],
    "dom_quiescent_ms": 250
  }
}
```

Runner: after each action, wait specifically for matching URL
patterns to complete, not generic `inflight === 0`. Solves
cascading dropdowns + the long-poll-keeps-inflight-nonzero case.

### 4. Context recording enrichment (days 3-4)

Already captured in fingerprint: testid, id, role, accessible_name,
text, css_path, xpath, ancestor_chain (5 deep), landmark, bbox.

Add per-action snapshot:
- Section context (H2/H3 ancestor, `<legend>`, `[aria-label]`).
- Sibling field state (what's already filled in this form).
- Validation messages visible at action time.
- Pre/post AX-tree snippet (form/section scope, not whole page).

Trade-off: ~5-50KB per action. ~500KB for a 19-step skill.
Acceptable. Used by annotate-LLM for better naming/disambiguation,
and by L4 vision-LLM (below) as grounding.

### 5. `repeat_for_each` annotation (days 4-5)

Annotate-time detection of contiguous spans where each action
targets a similar element in a similar container with only the
index/value varying. Mark as:

```json
{
  "kind": "repeat_for_each",
  "param": "selected_assets",
  "body": [
    { "click": "btn-open-asset-picker" },
    { "fill": "input-asset-search", "value": "{item}" },
    { "click": "checkbox-asset-{item}" }
  ]
}
```

At replay, iterate over the list param and template the body per
iteration. Solves multi-select cardinality (3 recorded checkboxes
→ list of N at replay).

### 6. `depends_on` params (half-day)

```json
{"name": "state", "type": "enum", "depends_on": "country"}
```

Runner behavior: after the dependent click, wait for the dependency
network signal, then `select_option(value)` against the *current*
options. The recorded testid for the dependent is treated as a
hint, not a target. Solves cascading enums (Country/State).

### 7. Vision-LLM L4 fallback (days 5-6)

Replace human-only L4 with a tier:
- **L4a vision-LLM** — sends current screenshot + recorded intent
  + top 5 candidate elements + step's `expected_signals`. Asks
  Claude Sonnet 4.6 vision for `{ action, locator }`. Cost
  ~$0.02/call. Only fires when L1/L2/L3 + post-conditions all
  failed.
- **L4b human** — only if L4a returns low-confidence.

Same path handles search-returned-multiple — vision picks the row
semantically matching the recording's intent.

### 8. Per-step post-conditions (days 6-7)

Each step gets a declarative assertion that the runner verifies
*after* the action:

```json
{
  "assert_after": [
    {"kind": "visible", "testid": "layout-editor"},
    {"kind": "count_gte", "selector": "[data-testid^='slot-']", "n": 4}
  ]
}
```

If post-condition fails, the step is treated as failed even if the
click "succeeded." Triggers recovery (heal → L4a vision → L4b
operator). Generated at annotate time from the post-action
AX-tree snapshot.

### 9. Disambiguation-hint persistence (days 7-8)

When the operator resolves an `ambiguous_target` by picking a
candidate, record the picked element's visible features (text,
position, neighbors) onto the step as a `disambiguation_hint`.
Next replay scores candidates against the hint before falling to
operator interrupt.

This is the **first real learning loop** — operator overrides
graduate to skill annotations automatically.

### 10. Known-blocker handlers as runner-level recovery (rolling)

Not skills; runner helpers that fire on specific signals:
- `dismiss_validation_modal` — fires when a modal with role=alertdialog
  appears mid-step.
- `accept_cookie_banner` — fires when `[data-testid='cookie-banner']`
  appears.
- `handle_session_timeout` — fires when an auth-loss probe trips.

Grow opportunistically as we hit them in real-portal runs.

## What's explicitly NOT in this sprint

- Chunking traces into atomic skills.
- Cross-skill composition planner.
- Skill versioning compatibility matrix.
- Operator review UI for chunked skills.
- All deferred until we have failure data from the real portal to
  justify the rewrite.

## Questions for review

1. **Order.** Is the day-1-2-3 order optimal? Pre-flight + 2FA
   first because it's the most concrete operator complaint that
   blocks every replay. Observability before any deeper change so
   we can measure. Network signals next because they unlock both
   cascading dropdowns and lag. Anything you'd reorder?

2. **`expected_signals` design.** Is per-step network-pattern
   matching the right granularity, or should we match at coarser
   "this step needs the State dropdown to be repopulated" level
   (semantic, not URL-based)? The semantic version requires more
   annotate-LLM smarts but is more portal-portable.

3. **`repeat_for_each` detection signal.** Right now I'd detect
   "N contiguous actions with same role+container+only-index-
   varying" deterministically. Is that sensitive enough? Or
   should we rely on LLM at annotate time to flag loops? My
   instinct: deterministic first pass + LLM second pass, but
   maybe LLM alone is fine since it's offline + one-shot.

4. **Vision-LLM L4a economics.** I claimed ~$0.02/call and
   ~5% of steps escalating → ~$0.05/run for a 30-step skill.
   Is that realistic for Claude Sonnet 4.6 vision? What's the
   right per-call budget cap (latency + cost) before we should
   escalate to human?

5. **Disambiguation-hint feature set.** What's the minimal
   feature set that captures "this row, not that row" robustly?
   Text + position + neighbors is my guess. Anything missing?

6. **The composition deferral.** Am I right that the cost of
   the full chunker+composition rewrite outweighs the additive
   path's coverage gap? Or is there a hybrid where we do
   chunker-only (no cross-skill planning) as part of this sprint
   so the *recording* gets cleaner without changing the
   *planner*?

7. **What am I not seeing?** Honest reviewer angle: what's the
   class of failure this sprint doesn't address that an
   experienced person would flag? Some candidates I worry about:
   - Iframes / shadow DOM (already partially handled in
     fingerprints but untested on real portal).
   - Portals where testids are not stable / not present.
   - Auth flows that involve a popup window (OAuth/SSO).
   - File uploads with content-validation that takes >10s.
   - Drag-and-drop interactions.

Please push back on anything that feels wrong. The goal of this
review is to surface blind spots, not to validate.
