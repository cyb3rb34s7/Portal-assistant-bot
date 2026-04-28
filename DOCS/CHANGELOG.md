# CurationPilot — Changelog

> Date-wise log of what changed, why, which files, and the commit id.
> Newest entries at the top. Append-only.

> **Maintenance rule.** Update at the end of every working session,
> *before* committing the actual change. The changelog entry is part of
> the change.

---

## 2026-04-28

### Phase 5 complete: end-to-end CLI run with real Groq + real Chrome works

**Commit:** _(this commit)_

A single natural-language CLI prompt now drives the full pipeline:
intake -> Groq planner picks correct skill from 3-skill library ->
auto-approved plan -> real Chrome over CDP replays the recorded
trace via `pilot.skill_runner` -> portal ends in the expected state.
Verified for both `featured-row` and `grid-2x2` layouts with
different operator-supplied comments.

**Files added:**
- `skills/curate_featured_row.json` (renamed from curate_layout) +
  `.v2.json` sidecar
- `skills/curate_grid_2x2.json` + `.v2.json` sidecar
- `skills/curate_carousel.json` + `.v2.json` sidecar (5 slots)
- `scripts/e2e_cli_run.py` — Phase 5 driver: launches headless
  Chromium with CDP at :9222, resets portal localStorage, invokes
  the agent CLI, asserts the `applied-layouts` list contains the
  layout the goal asked for.

**Updated:**
- `DOCS/CONTEXT.md` §3 with Phase 1-5 outcomes.

**Bench scores (5-case planner eval, 2026-04-28):** 5/5.
- P1 (featured-row): plan + correct skill + correct slot params.
- P2 (grid-2x2): plan + correct skill + correct slot params.
- P3 (carousel): plan + correct skill (5-slot variant).
- C1 (no layout specified): clarify -- "Which layout would you
  like to use for curation?"
- C2 (no CSV attached): clarify -- precise questions about layout +
  upload, no hallucinated content_ids.

### Architectural decision AD-002: v1 ships as web app, v2 wraps in Electron

**Commit:** _(this commit, docs only)_

**Files changed:**
- `DOCS/PRODUCT_PLAN.md` — added §3.4 "UI delivery strategy — v1 Web,
  v2 Electron"; updated §3.1 topology diagrams (separate v1 web vs v2
  Electron pictures); updated §3.2 process model table for both
  versions; rewrote §6.1 scope and §6.3 Week 3 + Week 4 to target a
  React web app instead of Electron, plus a new V2 phase for the
  Electron wrap; revised §11 (Adopt / Reject) to reflect the v1=Web
  / v2=Electron stack split, the `HostBridge` seam, FastAPI web
  server, native WebSocket; explicitly rejected Next.js, xstate,
  socket.io, Vercel AI SDK for v1, and Electron in v1.
- `DOCS/CONTEXT.md` — added §8 "Architectural decisions log" with
  AD-002 (this decision) and AD-001 (the AIClient/no-framework
  decision from 2026-04-24, captured retrospectively); updated §5
  "Next up" to reflect the Week 3 = web app and Week 4 = polish +
  Electron-readiness audit, with a separate v2 phase.
- `DOCS/CHANGELOG.md` — this entry.

**Why.** Operator (user) raised the concern that Electron development
is harder to maintain and the long-term support story is uncertain.
The realisation that "Electron's renderer is just our React app
hosted in Chromium" means whatever we write for v1's browser ships
into v2's Electron renderer unchanged — provided we put the desktop-
vs-web seam behind a single `HostBridge` interface from day one.

**Effect on Week 1 (just shipped).** None. The schemas, intake,
planner, clarify, orchestrator, server (stdio), CLI, and tests are
all delivery-channel-agnostic. The web app is a new sibling of the
existing stdio JSON-RPC surface; both wrap the same `Orchestrator`.

**No code changes in this commit.** Code work for AD-002 starts in
the next commit (Week 3 scaffold + `web_server.py`).

### Week 1 v1 agent build complete (Checkpoints A + B + C)

**Commit:** _(this commit)_

**Files added:**

*Schemas + protocol (Checkpoint A)*
- `DOCS/PROTOCOL.md` — JSON-RPC NDJSON wire spec: envelope shape,
  full command + event catalog, error taxonomy, sequencing rules,
  capability negotiation.
- `pilot/agent/schemas/__init__.py` — schemas package surface.
- `pilot/agent/schemas/domain.py` — `ContentItem`, `ScheduleWindow`,
  `ResolvedFile`, `IntakeEntities`, `PlanStep`, `Plan`,
  `DestructiveAction`, `SkillSummary`.
- `pilot/agent/schemas/protocol.py` — full set of
  `MessageEnvelope` + typed `HostCommand` / `AgentEvent` payloads
  (`TaskSubmit`, `ClarifyAsk`, `ClarifyAnswer`, `PlanProposed`,
  `PlanApprove`, `PlanReject`, `StepStarted/Progress/Succeeded/Failed`,
  `Paused`, `PauseResolve`, `ReportReady`, `TaskCompleted`,
  `TaskFailed`, `TaskCancel`, `TaskCancelled`, `AgentReady`,
  `AgentLog`, `AgentHeartbeat`) + `parse_host_command()`.
- `pilot/agent/schemas/skill.py` — v2 `SkillFile` with `SkillParameter`,
  `SuccessAssertion`, `DestructiveActionSpec`, plus `upgrade_v1_to_v2()`
  field-rename migrator (see P-006 below).
- `pilot/agent/schemas/portal_context.py` — `PortalContext` with
  glossary, page map, field conventions, danger zones, and
  `render_for_prompt()` for compact LLM grounding.
- `portals/sample_portal/context.yaml` — hand-authored grounding for
  the existing test portal.

*Mock client + intake + planner (Checkpoint B)*
- `pilot/agent/ai_client/adapters/mock.py` — `MockClient` with
  predicate-based response routing for unit tests; registered as
  `"mock"`.
- `pilot/agent/ai_client/registry.py` — added `mock` to the
  registered adapters.
- `pilot/agent/intake.py` — deterministic prepass (PPTX shape text via
  zipfile, CSV `csv` module, folder scan, asset_id `A-NNNN` regex,
  ISO date regex) + LLM refinement that fills gaps without overwriting.
- `pilot/agent/planner.py` — `_PlanOrClarify` discriminated output,
  validates skill ids and param names, demotes plans with unknown
  params, emits clarify questions when confidence is too low.

*Clarify + reporter + orchestrator + server + CLI (Checkpoint C)*
- `pilot/agent/clarify.py` — `ClarifyState` with `max_rounds=3`
  budget, `to_goal_addendum()` to fold answers into next planner call.
- `pilot/agent/reporter.py` — deterministic markdown writer with
  optional LLM headline + paragraphs; falls back silently if the
  LLM call fails.
- `pilot/agent/orchestrator.py` — `Orchestrator` state machine
  driving the full flow, plus `StepExecutor` interface and
  `FakeExecutor` for tests; pause/retry/skip/abort handling on step
  failure; one-task-at-a-time discipline.
- `pilot/agent/server.py` — stdio NDJSON JSON-RPC server with stdin
  reader, stdout writer, heartbeat task, capability announcement.
- `pilot/agent/cli.py` — Rich-based terminal driver with `do`,
  `intake`, `plan` subcommands; exposes `--auto-approve`,
  `--no-llm-intake`, `--portal`, `--client` flags.

*Tests + eval*
- `tests/agent/__init__.py`
- `tests/agent/test_smoke_single_item.py` — single-item end-to-end
  with the mock client, asserts event sequence + report on disk.
- `tests/agent/test_integration_multi_item.py` — 3-item CSV with one
  configured-to-fail step that succeeds on retry.
- `tests/agent/test_groq_live.py` — live Groq integration; auto-skips
  without `GROQ_API_KEY`.
- `scripts/eval_planner.py` — multi-(client, model) comparison harness.

**Verified locally (2026-04-28):**
- Mock smoke + integration tests: 2 passed in 0.81s.
- Live Groq integration test: 1 passed in 0.89s — planner extracted
  `content_id=A-9001`, `layout_row=1`, `layout_position=2`.
- Live Groq CLI `plan` against the real `skills/curate_one_item.json`:
  all 7 v1 parameters extracted from one English sentence.

**Why.** Week 1 of the v1 agent build per `DOCS/PRODUCT_PLAN.md`. The
deterministic core (Phase 0) is unchanged; this layer adds the
LLM-at-the-edges intake/planner/clarify/reporter loop and the
JSON-RPC + CLI control surfaces.

### `upgrade_v1_to_v2` field-rename migrator (P-006 fix)

Same commit as Week 1 above. First cut of `upgrade_v1_to_v2` only
stamped `schema_version: 2` without translating the v1 top-level
`params` field into v2 `parameters`; the existing
`skills/curate_one_item.json` loaded with zero declared parameters,
which made the planner strip every parameter as "unknown". Rewrote the
migrator to rename + reshape each entry and synthesize a missing `id`
from `name`. See `DOCS/CONTEXT.md` §6 P-006 for the full root cause +
lesson.

### Bootstrap CONTEXT.md and CHANGELOG.md, kick off Week 1 build

**Commit:** _(this commit)_

**Files added:**
- `DOCS/CONTEXT.md` — single source of truth for project state, recently
  completed work, in-progress items, what's next, and a permanent
  problems-and-solutions log (P-001 through P-005).
- `DOCS/CHANGELOG.md` — this file. Date-wise diffs with commit ids.

**Why.** User requested a stable handoff format so a dropped session can
be resumed. Future sessions read CONTEXT.md first, then this changelog
in reverse-chronological order to reconstruct state.

### Live verification: Groq adapter end-to-end (no commit)

**Verified.** Three modes against live Groq API with a free-tier key
(since revoked):
- `client.complete()` → "Titan is the largest moon of Saturn." (166 ms,
  63 tokens, finish_reason=stop)
- `client.stream()` → 6 chunks streamed in order, finish_reason=stop
- `complete_structured()` → Pydantic-validated `CityFact(city='Paris',
  country='France', population_millions=12.2, famous_for=[...])` on
  first attempt, no retries needed.

**Why.** Confirm the AIClient layer works against real infrastructure
before depending on it for Week 1 stages.

---

## 2026-04-24

### `6ef3b1e` — Add AIClient protocol + Bedrock/Groq/OpenAI adapters, update plan

**Files added (`pilot/agent/`):**
- `__init__.py` — agent package public surface.
- `ai_client/__init__.py` — re-exports AIClient + helpers.
- `ai_client/base.py` — `AIClient` Protocol, `Message`, `ToolDef`,
  `ToolCall`, `Completion`, `StreamChunk`, `Usage` dataclasses.
  Optional `BaseAIClient` mixin.
- `ai_client/registry.py` — `register_client` / `get_client` factory
  with lazy adapter loading so missing provider SDKs only break their
  own adapter, not the package.
- `ai_client/structured.py` — `complete_structured()`: provider-agnostic
  JSON Schema + Pydantic + bounded retry. Replaces dependency on
  `instructor`.
- `ai_client/adapters/__init__.py` — adapter package.
- `ai_client/adapters/_openai_compat.py` — shared message/tool/response/
  stream translation for OpenAI-shape APIs.
- `ai_client/adapters/bedrock.py` — AWS Bedrock via Converse +
  ConverseStream APIs (multi-family: Claude, Llama, Nova, Mistral).
  Uses boto3 in a worker thread.
- `ai_client/adapters/openai.py` — Official `openai` async SDK; works
  against api.openai.com or any OAI-compatible base URL.
- `ai_client/adapters/groq.py` — Official `groq` async SDK (OAI-shape).
- `ai_client/adapters/custom_org.py` — skeleton for the user's
  organisation LLM. Registration commented out until real client is
  dropped in.

**Files changed:**
- `DOCS/PRODUCT_PLAN.md` — added §11 "Tech Stack & Framework Decisions"
  (adopt/reject tables, AIClient detail, per-stage routing, security,
  risk to monitor). Renumbered Open Questions → §12, Appendix → §13.
  Updated Week 1 deliverables to mark ai_client done.

**Why.** User chose to own the LLM client layer rather than depend on
LiteLLM (vulnerability surface, custom-format incompatibility) or
agent frameworks (assume LLM-in-the-loop, our executor stays
deterministic). The AIClient Protocol is the abstraction every later
agent stage codes against.

**Verification.** All 11 files compile, all three adapters register on
import, Protocol shape (`name`, `default_model`, `complete`, `stream`,
`close`) confirmed on each adapter class.

---

## 2026-04-23

### `3501774` — Add PRODUCT_PLAN.md with v1/v2 roadmap, architecture, UX

**Files added:**
- `DOCS/PRODUCT_PLAN.md` (889 lines) — sections: vision, design
  principles, architecture (process model, attached-browser strategy),
  data model (intelligent skill schema, portal context schema),
  JSON-RPC event protocol overview, V1 plan (4-week schedule, exit
  gates per week, risks), V2 plan (deferred features), user journey,
  visual layout sketches, capabilities/non-features, open questions,
  appendix (directory layout, terminology).

**Why.** User asked for a document that captures the agreed plan after
discussions on assistant scope, agentic UX, attached-browser approach,
and v1 vs. v2 boundaries.

### `1450e6f` — Fix teach recording: decouple binding callback from Playwright calls

**Files changed:**
- `pilot/teach.py` — refactored to use a `deque[str]` for raw payloads.
  `on_capture` (running inside the Playwright binding callback) now
  only appends the JSON payload string. New `_drain_pending()` runs on
  the main pumping loop and calls `_process_payload()`, which does the
  JSON parse, fingerprint validation, screenshot, disk write, and live
  render. Final drain before `_finalize()` so last-tick events aren't
  lost.

**Why.** Previous fix (`b1b0497`) pumped the event loop so callbacks
finally fired — which then immediately deadlocked on `page.screenshot()`
called from inside the binding callback. py-spy stack:
```
screenshot (...:9819)
_screenshot (teach.py:187)
_on_event (teach.py:174)
on_capture (teach.py:96)
wrapper_func (...impl_to_api_mapping.py:123)
```
Sync Playwright reenters its own event loop when methods are called
from inside a binding callback, blocking forever. See P-004 in
CONTEXT.md.

**Verification.** Live reproduction on this sandbox: 11 events captured
across two driving rounds, clean Ctrl+C finalize printing the summary
table. py-spy shows main thread idle on `select()` between ticks.
User confirmed it works on Windows.

### `b1b0497` — Fix teach recording: pump event loop + reload page + probe binding

**Files changed:**
- `pilot/teach.py` — replaced `time.sleep(0.25)` in `start()` wait loop
  with `page.wait_for_timeout(200)` to actively pump the sync_playwright
  event loop. Added `page.reload(wait_until="domcontentloaded")` after
  binding attachment so pre-existing tabs pick up the binding routing.
  Added an `__init_probe` event sent during setup; raises `RuntimeError`
  if it doesn't round-trip within ~1 second. Heartbeat log every 10s
  while idle.
- `pilot/overlay/grabber.js` — `DEBUG` flag now opt-in via
  `window.__cp_debug`. Promise-rejection logging added so future binding
  failures surface in DevTools console.

**Why.** Operator on Windows reported teach captured only one navigate
event from the probe-triggered reload, then silence. Three stacked bugs
(see P-002, P-003, P-004 in CONTEXT.md). This commit fixed P-002 and
P-003 but exposed P-004; that was fixed in `1450e6f`.

---

## Earlier (pre-conversation)

Phase 0 — deterministic teach/annotate/replay core, sample portal,
e2e + resilience tests. Predates this changelog; covered in
`DOCS/GUIDE.md` and `DOCS/requirement.md`.
