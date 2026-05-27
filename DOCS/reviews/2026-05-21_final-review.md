# 2026-05-21 — Final sprint review

Reviewer: independent subagent (general-purpose, gpt-5.5)
Branch reviewed: `feat/enterprise-portal-and-additive-sprint` at commit `9cb9c56`
Test baseline at review: 499/499 pass
Sprint scope: F-01..F-10 + WI-01..WI-50 (60 work items)

## Verdict

**Sprint largely complete. 4 issues found that need a follow-up batch.**

High-stakes spot checks all PASSED:
- WI-08 (SPA navigation collapse) — confirmed: click + caused-navigate emits ONE step with `effects.navigation`, no separate `goto`.
- WI-04 (idempotency) — confirmed: `allow_existing_header: false` on sample portal; runner's stable key wins on retry; XHR shim honors configured `header_name`; fetch shim handles `Request` objects.
- WI-17 (select_option) — confirmed: default `option_match_policy="exact_only"`; "US" cannot auto-select "UAE" without explicit alias.
- WI-19 (set_selection) — confirmed: runner verifies final chip set equals target via `final_equality_assertion` (default True) and fails structurally on mismatch.
- F-02 (sample portal retry safety) — confirmed: app generates per-call keys but runner's stable key overrides them at replay; mock backend's middleware reads the runner's key on both attempts.

## Issues requiring follow-up (prioritized)

### 1. Matrix-test honesty bugs

`tests/agent/test_wi_regression_matrix.py` constructs Pydantic models with kwargs that don't exist; Pydantic silently ignores them, tests still pass via unrelated assertions. The tests appear to verify behavior but don't.

- Line 403: `NetworkExpectation(match_mode="started_after", ...)` — `match_mode` is not a field. The assertion downstream is on `started_after_event` which IS set, so the test passes meaninglessly.
- Lines 426, 431: `ParamProvenance(source_step_index=2, ...)` and `TemplatePart(kind="literal", value="btn-open-")` — actual field names are `source_step` and `text`. Same silent-ignore + meaningless-pass pattern.

**Fix:** Correct the kwargs. Optionally tighten the audit-critical Pydantic models with `model_config = ConfigDict(extra="forbid")` so the next drift fails at construction.

### 2. STRUCTURAL_FIX_GUIDE.md doc drift

`DOCS/STRUCTURAL_FIX_GUIDE.md` line 167 claims `ParamProvenance.source` Literal includes `request_query | request_body`. Schema has `request_param` (singular).

**Fix:** Align the guide with the schema, OR split `request_param` into two Literal values (`request_query`, `request_body`) if the distinction matters semantically.

### 3. Hardcoded heuristics that remain in grabber.js

Surface through `PortalContext.wait_policy` (the F-08c pattern). Each is currently a literal in the JS file:

- `INPUT_DEBOUNCE_MS = 400` (line 23) — text-input debounce.
- `DOM_MUTATION_BURST_MS = 200` (line 293) — dom_mutation event debounce.
- `FALLBACK_WINDOW_MS = 50` (line 69) — History API wrapper "small budget" window. Still time-based attribution, the same class F-08a aimed to eliminate.
- Submenu-search loop cap `200` (lines 1263, 1278) for WI-40 hover reveal.
- ARIA-option capture cap `40` (line 2837) — inconsistent with the configurable `__cp_opts_cap` used at line 1917.

**Fix:** Five new `WaitPolicy` fields + grabber reads them via `window.__cp_*` globals set in `_ensure_watchers`.

### 4. L3 score bands still magic numbers

`pilot/agent/locator_repair.py:189-191`: `_DET_HIGH = 0.85`, `_DET_MEDIUM = 0.65`, `_DET_REFUSE_BELOW = 0.55`. The completion summary claims these are CLOSED via WI-26 RepairPolicy. They are now relegated to tiebreakers behind a structural gate — defensible, but the constants still drive the score-to-confidence mapping that the original audit flagged.

**Fix (pick one):**
- Delete the bands now that RepairPolicy is the gate, OR
- Formally document them as "audit-only tiebreaker, not safety gate" in code comments AND update the completion summary to reflect honest status.

## Non-blocking observations

- `_DESTRUCTIVE_ACTIONS` frozenset in `pilot/skill_runner.py:4517-4522` includes `click` broadly. Defensible; worth a config knob if a portal needs an override.
- `key_dom_signature` (F-06) is admittedly coarse — disclosed in the completion summary.
- The regression matrix is mostly a "WI exists in the schema" sentinel rather than behavior coverage. The dedicated per-WI test files carry the real coverage and are largely honest. 499 total tests pass cleanly.
- `ToastEffect` carries both `text_pattern` and `message_matcher` with overlapping semantics; no validator enforces exactly one is set. Hand-edited skills with both could silently pick the wrong one.
- `ModalEffect.opens_on_action=True` combined with legacy `kind="close"` has no validator; legacy + new field coexistence is intentional but undocumented.
- `SkillParamType` Literal declares `"file_path"` twice (Pydantic de-duplicates Literal at construction so no failure, but the source is misleading).

## Outstanding deferred items (disclosed by sprint, NOT bugs)

These were called out as explicit defers during the sprint — not blockers, but the next operator should know:

- WI-31: LLM overlays gate at validation but don't auto-correct hand-edited contradictions.
- WI-32: Cross-origin iframe → deferred-step diagnostic; closed shadow roots not pierced.
- WI-35 / WI-46: Don't discover pre-existing operator-opened tabs.
- WI-43 / WI-10: Need observed DOM/network transitions; portals with Promise-await intrinsic readiness need portal-context-declared waits.
- WI-47: Push body matching is substring; binary frames not introspected.
- WI-49: Only `noop_click` adapter ships; real portal adapters need per-portal registration.
- F-06: `key_dom_signature` is a hint, not a hard matcher.
- F-08c wait defaults remain as legacy fallback constants.

## Pointer

For the full per-WI completion log, see `DOCS/reviews/2026-05-21_droid-completion-summary.md`.
