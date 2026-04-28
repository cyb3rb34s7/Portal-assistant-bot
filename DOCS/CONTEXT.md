# CurationPilot — Project Context

> **Purpose of this file.** Single source of truth for "what is this project,
> what's done, what's in progress, what's next, and what mistakes we already
> made." If a session is dropped or a new contributor picks up, this is the
> first file to read. Pair with `DOCS/CHANGELOG.md` for date-wise diffs.

> **Maintenance rule.** Update this file at the end of every working session,
> *before* committing. Sections marked `[live]` must reflect the actual
> current state, not aspirations.

---

## 1. What is CurationPilot

A **desktop assistant** that performs portal workflows on behalf of operators.
The operator describes a goal in plain language (with optional file
attachments — PPT, CSV, folders), and the assistant:

1. Understands the goal against a library of recorded **skills** + a
   **per-portal context file**.
2. Asks a small number of disambiguating questions when needed.
3. Proposes an **auditable plan** the operator approves before any action.
4. Executes the plan via a **deterministic replay core** in a CDP-attached
   Chromium window the operator watches live.
5. Pauses on failure with recovery options; never silently continues.
6. Produces a post-run **report** (markdown + screenshots).

**Architectural commitment.** LLMs reason at the edges (intake, planner,
clarify, reporter, locator-repair-on-failure). The deterministic
`pilot/` runner executes recorded skills in the middle. This split keeps
auditability + reliability while still feeling agentic.

**Read first:**
- `DOCS/PRODUCT_PLAN.md` — full architecture, v1/v2 plan, user journey,
  visual layout, framework decisions
- `DOCS/GUIDE.md` — operator guide for teach/annotate/replay
- `DOCS/requirement.md` — original product requirements

---

## 2. Repository layout `[live]`

```
Portal-assistant-bot/
  pilot/                       deterministic runner core (Phase 0, complete)
    teach.py                   record interactions via grabber.js + CDP binding
    annotate.py                trace -> parameterised skill JSON
    skill_runner.py            replay with 4-level locator fallback
    runner.py                  task sequencing primitives
    audit.py                   per-session audit log
    overlay/grabber.js         in-page event capture
    adapters/                  portal-specific glue
    agent/                     v1 agent layer (in progress)
      __init__.py
      ai_client/               LLM provider abstraction (DONE)
        base.py                AIClient Protocol + types
        registry.py            get_client / register_client
        structured.py          complete_structured (provider-agnostic)
        adapters/
          _openai_compat.py    shared OAI-shape translation
          bedrock.py           AWS Bedrock (Converse + ConverseStream)
          openai.py            OpenAI async SDK / OAI-compatible URL
          groq.py              Groq async SDK
          custom_org.py        skeleton for org LLM (not registered yet)
  sample_portal/               React+Vite test portal
  skills/                      recorded + annotated workflows
  sessions/                    per-run trace + screenshots + report
  DOCS/
    PRODUCT_PLAN.md            single architecture / plan document
    PROTOCOL.md                JSON-RPC event protocol (Checkpoint A)
    CONTEXT.md                 *this file*
    CHANGELOG.md               date-wise diffs with commit ids
    GUIDE.md                   operator guide
    requirement.md             original requirements
```

---

## 3. Recently completed `[live]`

### Phase 0 — Deterministic runner (pre-existing)

- Sample portal with `dashboard`, `media-assets`, `curation` routes
- Teach / annotate / replay loop with 4-level locator fallback
- E2E + resilience tests passing on Linux

### Teach-mode reentrancy bug (commits `b1b0497`, `1450e6f`)

The first live-teach test on Windows captured only one navigate event. Two
stacked bugs:

1. **Event-loop starvation** — `start()` had `time.sleep(0.25)` in its wait
   loop. `sync_playwright` dispatches binding callbacks on the main-thread
   event loop. Sleeping the main thread starved the loop; events queued
   forever. Fix: replace with `page.wait_for_timeout(200)` to actively
   pump the loop.
2. **`sync_playwright` reentrancy deadlock** — `_on_event` called
   `page.screenshot()` from inside the binding callback. Calling a sync
   Playwright method from inside a binding callback reenters the event
   loop and deadlocks. py-spy showed the main thread blocked inside
   `screenshot()` with `_on_event` and `on_capture` on the stack. Fix:
   binding callback only appends raw payload to a `deque`; main pumping
   loop drains and processes them where Playwright calls are safe.

**Verified live** on this sandbox: 11 events captured + clean Ctrl+C
finalize, then user confirmed it works on Windows.

### Product plan + framework decisions (commits `3501774`, `6ef3b1e`)

- `DOCS/PRODUCT_PLAN.md` written (12+ sections, user journey, visual
  layouts, capabilities, framework decisions).
- AIClient protocol + Bedrock/OpenAI/Groq adapters + custom_org skeleton.
- Provider-agnostic `complete_structured()` helper.
- Decided NOT to use LiteLLM (vulnerability surface, custom-format
  unfriendly), agent frameworks (LangGraph, OpenAI Agents SDK, etc.;
  they assume LLM-in-execution-loop), or `instructor`.

### Live Groq verification

Three modes verified end-to-end against the real Groq API with a
free-tier key:
- `complete()` — "Titan is the largest moon of Saturn." 166 ms, 63 tokens
- `stream()` — 6 chunks streamed correctly
- `complete_structured()` — Pydantic `CityFact` validated on first try

Free-tier key was revoked after the test (and is in chat history; do not
reuse).

---

## 4. In progress `[live]`

**Week 1 — v1 agent build COMPLETE. All deliverables shipped + verified
against both the mock client and live Groq.**

- [x] Checkpoint A — schemas + protocol doc
  - `DOCS/PROTOCOL.md` — JSON-RPC NDJSON wire spec (envelope, command
    & event catalog, error taxonomy, sequencing rules)
  - `pilot/agent/schemas/{domain,protocol,skill,portal_context}.py`
  - `portals/sample_portal/context.yaml`
  - Skill schema v2 + v1→v2 migrator (handles `params`→`parameters`
    rename)
- [x] Checkpoint B — intake + planner + mock client
  - `pilot/agent/ai_client/adapters/mock.py` — programmable
    `MockClient` with predicate routing
  - `pilot/agent/intake.py` — deterministic prepass (PPTX via zipfile,
    CSV, folder scan, asset_id/date regex) + LLM refinement, falls
    back gracefully when LLM unavailable
  - `pilot/agent/planner.py` — discriminated `_PlanOrClarify` output;
    validates skill ids + param names; emits clarify questions on
    failure
- [x] Checkpoint C — clarify + orchestrator + server + CLI
  - `pilot/agent/clarify.py` — `ClarifyState` with 3-round budget
    + `to_goal_addendum()` to fold answers back into the planner input
  - `pilot/agent/reporter.py` — deterministic markdown + optional LLM
    headline/paragraphs (falls back silently)
  - `pilot/agent/orchestrator.py` — `Orchestrator` state machine
    (intake → clarify-loop → plan → approve → execute → report) +
    `FakeExecutor` for tests + pause/retry handling on step failure
  - `pilot/agent/server.py` — stdio NDJSON JSON-RPC server with
    heartbeat
  - `pilot/agent/cli.py` — Rich-based terminal UI with `do` /
    `intake` / `plan` subcommands
- [x] Eval / prompt-test harnesses
  - `scripts/eval_planner.py` — multi-(client, model) comparison
- [x] Smoke + integration tests
  - `tests/agent/test_smoke_single_item.py` (mock, all stages, 0.4s)
  - `tests/agent/test_integration_multi_item.py` (mock, 3 items, one
    pause+retry, 0.4s)
  - `tests/agent/test_groq_live.py` (skipped without `GROQ_API_KEY`)

**Live verification (2026-04-28):**
- Smoke + integration tests: PASS (0.81s combined)
- Live Groq integration test: PASS (0.89s)
- Live Groq CLI `plan` against the existing `skills/curate_one_item.json`:
  PASS — all 7 v1 parameters extracted correctly from a single
  natural-language sentence including dates.

**Phase 1-5 (curation workflow E2E proof, 2026-04-28):**
- Phase 1: Sample portal rebuilt with Upload + Curation tabs, 3
  layouts (grid-2x2 4 slots, featured-row 4, carousel 5), CSV-driven
  contents and per-slot image upload. All 3 layouts driven end-to-end
  via Playwright operator simulator.
- Phase 2: `pilot/agent/executor_real.py` wires the orchestrator to
  the existing `pilot.skill_runner`. CLI `--executor real` flag.
- Phase 3: `scripts/teach_workflow.py` records 3 skills
  (curate_featured_row, curate_grid_2x2, curate_carousel) end-to-end
  via headless Chromium with CDP, runs auto-annotate, and replays
  the recorded skill back against the portal — replay green at all
  L1 exact locators (with two L2 semantic fallbacks where react
  re-rendered the testid).
- Phase 4: `pilot/agent/annotate_llm.py` uses Groq to enrich each v1
  skill into a `.v2.json` sidecar with semantic param names, source
  hints, file-path types, descriptions, preconditions, destructive
  flags. Planner cold-start eval bench (5 cases) scored 5/5: 3
  layouts produce correct plans against fresh CSVs with the right
  skill picked from the 3-skill library; 2 ambiguous goals ("no
  layout specified", "no CSV attached") correctly emit precise
  clarify questions.
- Phase 5: End-to-end CLI run via `pilot.agent.cli do "..." --client
  groq --executor real --auto-approve`. Both `featured-row` and
  `grid-2x2` natural-language goals planned correctly, replayed
  through real Chromium over CDP, and ended in the expected portal
  state (`<layout> — N slots — "<comment>"` visible in the Applied
  list).
- Phase 6 (hostile/operator-grade testing): 11 PASS / 1 PASS-with-
  caveat / 1 model-quality finding across 13 sub-tests covering
  carousel E2E, real intake-LLM, malformed CSV (missing column / 2
  rows for 4 slots / duplicates / quoted fields), missing image on
  disk, deliberate locator drift, hallucinated-id-in-goal,
  contradictory goal, pre-existing portal state, mid-run cancel,
  clarification quality bench, and 3-layout stress. Found and fixed
  two real bugs along the way: planner hallucinating file paths from
  goal-only references (tightened system prompt), and inconsistent
  carousel comment param name across skills (renamed
  carousel_comment -> layout_comment for cross-skill consistency).

Approach (held throughout):
- Pydantic schemas for every LLM input/output and every JSON-RPC payload.
- Mock AIClient for unit tests; real Groq for integration tests, gated
  behind `GROQ_API_KEY`.
- Hand-written orchestrator (~430 LOC). No agent framework.

### Local operator demo against Groq + plumbing polish (2026-04-29)

First end-to-end run of the rebuilt agent against a live LLM,
driven by the operator (not by an automated harness). Recorded a
fresh skill (`my_curation`, 17 steps) using `featured-row` against
`batch.csv`, annotated with `--auto`, enriched via Groq's
`annotate_skill_llm.py` (got semantic param names, alias map,
destructive flags, success assertions), then replayed twice from
natural-language goals — both **end-to-end success** with 14 of 17
sub-steps at L1 exact and **2 at L2 semantic fallback** (the first
production L2 hits without operator awareness — exactly the design
intent).

Two bugs found and fixed live:

- **P-009** — `'Skill' object has no attribute 'id'`. The self-heal
  commit's `_run_skill_sync` worker used `skill.id` for write-back
  path resolution, but `skill` in that scope is the v1
  `pilot.skill_models.Skill` (no id field). Fix: resolve
  `skill_path` in `execute()` (where the v2 SkillFile is in scope)
  and pass it to the worker. ~6 LOC.
- **P-010** — Planner picks a 5-slot skill (carousel) when CSV has
  4 rows and crashes at execute time with a missing-required-param
  error. Planner's deterministic post-validation checks "unknown
  params" but NOT "missing required params". Architectural fix
  designed (AD-005); not yet implemented.

Plumbing improvements landed:

- `python-dotenv` wired into pilot package import; `.env`
  gitignored, `.env.example` committed
- `DEFAULT_CDP_ENDPOINT` flipped from `localhost:9222` to
  `127.0.0.1:9222` (Chrome's CDP binds to IPv4 only; Windows
  resolves localhost to `::1` first)
- `pyproject.toml` declares `python-dotenv` + `pyyaml` core, with
  `[groq]` / `[openai]` / `[bedrock]` / `[all-llm]` extras
- New helper scripts: `scripts/inspect_skill.py`,
  `scripts/replay_nl.cmd`, `scripts/reset_portal.py` — cmd-friendly
  one-liners for the teach/annotate/replay cycle

**Architectural decisions captured today** — see §8 for full text:

- **AD-003** — Local secrets via dotenv
- **AD-004** — Skills are recordings, not inferences (the planner
  shall not "guess" parameters for a flow it has never been taught)
- **AD-005** — Planning-time coverage validation (next-up work)
- **AD-006** — Layout-as-parameter as a real gap; loop-aware skills
  are the right long-term fix; portal-context routing is the
  shippable interim

### Code-review bug-fix sweep + L3 self-heal (2026-04-28)

Architecture review of the agent layer surfaced 11 issues; **9 fixed
in one pass** (issues 1, 2, 3, 4, 5, 7, 8, 12, 16 from the review log)
plus a real portal bug (#20: stale file inputs across layout swap)
discovered during the new operator E2E test. Behaviour-preserving
refactor; all existing tests stay green.

Then **L3 self-heal landed as the runner's locator-repair stage**.
Architecture: deterministic-by-default with confidence bands, post-
condition signature compare, alternate-fingerprint persistence onto
the skill JSON, and a `StepHealedEvent` typed protocol event so
operators see what got healed. LLM-pick backend ships behind a
`make_repair_for_runner(client=...)` helper for when the internal LLM
is wired in; without a client, the deterministic backend is used.

Adds:
- `pilot/agent/locator_repair.py` (`LocatorRepair`, `HealResult`).
- `tests/agent/test_locator_repair.py` (7 cases).
- `scripts/operator_test_suite.py` (13 Playwright scenarios; 13/13
  PASS against the rebuilt portal).

Updates: `pilot/skill_runner.py` (3-tuple resolver, alternates
lookup, post-condition check), `pilot/agent/executor_real.py` (CDP
reuse, base_url default, file-path pre-check, skill JSON write-back),
`pilot/agent/orchestrator.py` (heals plumbing, `StepHealedEvent`
emission, executor.close in finally), `pilot/agent/planner.py`
(alias map off global, errors sink), `pilot/agent/schemas/skill.py`
(example -> default_hint preserve), `pilot/models.py`
(`ToolResult.healed`), `pilot/skill_models.py`
(`ElementFingerprint.alternates`), `pilot/agent/schemas/protocol.py`
(`StepHealedEvent`), `pilot/agent/annotate_llm.py` (keyword-based
destructive kind), `sample_portal/src/pages/Curation/index.jsx`
(layout-keyed SlotCard).

**Verified:** 9/9 unit + integration tests in 0.92s; 13/13 operator
E2E. Working tree clean before commit.

---

## 5. Next up `[live]`

**Immediate next work (sized small, ship-this-week)**:

1. **AD-005 — planner coverage validation.** Extend
   `pilot/agent/planner.py::_validate_planner_output` to check
   every `required: true` parameter on the chosen skill has a
   value in `cs.params`. Missing -> demote plan to clarify.
   Add a defense-in-depth pre-flight in `executor_real.py::execute`
   that returns `error_kind=missing_required_params` with the
   missing list before the runner is invoked. ~40 LOC. Closes
   P-010 cleanly.
2. **AD-006 — Option 1: portal-context routing.** Extend
   `portals/<id>/context.yaml` with a `flows[*].by_layout` map and
   teach the planner to route layout -> skill from it instead of
   guessing. ~30 LOC + YAML. Lets the operator say "carousel" and
   the planner picks `curate_carousel` deterministically.
3. **Real-portal smoke test of self-heal write-back.** Take an
   existing skill, corrupt one step's testid + element_id +
   css_path + xpath, replay, assert L3 fired with high confidence
   AND the alternate fingerprint was persisted to the skill JSON
   AND the second replay hits L1. Promotes the manual heal demo
   into automated regression.

After Week 1 ships:

**Week 2** — intelligent-skill metadata pass during `annotate`, plan-
preview UX in CLI, mid-loop pause hardening, dogfood on real workflows.

**Week 3** — **React web app shell** (`curationpilot-app/`): React 18 +
TS strict + Vite 5 + Tailwind v4 + shadcn/ui, talks to a new
`pilot/agent/web_server.py` (FastAPI wrapper around the existing
Orchestrator) over WebSocket + REST. **HostBridge interface** in
`src/host/bridge.ts` with `bridge.web.ts` (v1 impl) and
`bridge.electron.ts` (v2 stub) so v2 is a drop-in port. See
`DOCS/PRODUCT_PLAN.md` §3.4 for the full decision.

**Week 4** — Markdown report viewer in the web app, session history,
crash-recovery polish, dogfood, **Electron-readiness audit** (every
browser-only API must go through `HostBridge`). v1 ships as
`pip install curationpilot` + `pilot serve` opens a browser tab — no
installer.

**v2 (after v1 dogfood)** — Electron wrap. Same React renderer, fill in
`bridge.electron.ts`, electron-builder, code signing, macOS
notarisation. **No changes** to React, Python agent, schemas, or
protocol — the §3.4 split is exactly so this is a port not a rewrite.

V2 deferred features list lives in `DOCS/PRODUCT_PLAN.md` §7.

---

## 6. Problems faced & solutions `[append-only]`

> **Rule.** When a non-trivial bug is found, document it here with the
> root cause + fix + commit id. Never delete entries; future-you will
> need them. New entries go at the top.

### P-010 — Planner picks an over-large skill when CSV is short of rows (2026-04-29)

**Symptom.** Operator tricked the system: ran the agent with a
goal asking for `carousel` layout (5 slots) but a CSV with only 4
rows. Planner picked `curate_carousel` (12 declared params, all
required) and emitted a plan with `slot_5_content_id` and
`slot_5_image_file_path` absent. Replay started, ran 13 sub-steps
fine, and crashed at step 14:
``SkillExecutionError: Step 14 needs parameter 'slot_5_content_select'
but it was not provided``. Operator only saw retry/skip/abort; none
were the right answer.

**Root cause.** The planner's deterministic post-validation in
`pilot/agent/planner.py::_validate_planner_output` checks "unknown
params" (strips them) but does NOT check "missing required params"
against the chosen skill's declared parameters. So a plan with an
unfillable required slot got approved and crashed at execute time.

**Fix.** Designed in AD-005 (see §8). Two layers:

- **Planner-side** — extend `_validate_planner_output` to confirm
  every `required: true` parameter on each `cs.params` is present.
  Missing -> demote plan to clarify with a question like "carousel
  needs 5 content+image pairs but the CSV has 4 rows; provide a
  5th, or pick a 4-slot layout?"
- **Executor pre-flight** — in `executor_real.py::execute`, walk
  the v1 skill's recorded steps and confirm each `param_binding.name`
  has a value in `step.params` (post alias-translation). If not,
  return `StepResult(error_kind="missing_required_params", ...)`
  with the missing list — clean error, not a stack trace.

**Lesson.** **A planner that validates "unknown params" but not
"missing required params" is half-validated.** Symmetric coverage
is the principle: every check that the LLM-emitted plan must pass
should have a deterministic post-LLM equivalent, not just the
positive-side ones. Asymmetric validators leak errors into the
runtime.

### P-009 — `'Skill' object has no attribute 'id'` after a successful replay (2026-04-29)

**Symptom.** Replay completed cleanly (17/17 sub-steps `ok`,
banner verified, applied list updated), then the agent printed
``agent_internal_error: AttributeError: 'Skill' object has no
attribute 'id'`` and `task.failed`.

**Root cause.** In yesterday's self-heal commit
(`Self-heal L3 + agent-layer bug-fix sweep + operator E2E suite`),
the `_run_skill_sync` worker function in `executor_real.py` does
post-run aggregation that includes "persist healed alternates back
to the skill JSON". For path resolution it called
``skill_path = self._locate_skill_file(skill.id)`` — but `skill` in
that scope is the **v1 `pilot.skill_models.Skill`** (no `id`
field), not the v2 `pilot.agent.schemas.skill.SkillFile` from the
outer `execute()` method. Worker thread doesn't have access to the
v2 SkillFile.

**Fix.** Resolve `skill_path` in `execute()` (where the v2
SkillFile is in scope) and thread it into the worker as a parameter:
``await loop.run_in_executor(self._pool, self._run_skill_sync,
v1_skill, runtime_params, skill.base_url, skill_path)``. Worker
no longer needs to introspect the skill. ~6 LOC change.

**Lesson.** **A method that runs in a worker thread should be
passed already-resolved values, not asked to re-derive them from
the worker's own scope.** When refactoring code across an async/sync
boundary, variables that look right (`skill.something`) may refer
to different types on the two sides — the worker's `skill` is the
v1 `Skill`, the outer scope's is the v2 `SkillFile`. Use distinct
parameter names if the types matter (`v1_skill`, `skill_file`).

### P-008 — React reconciliation reused a stale file input across layout swap (2026-04-28)

**Symptom.** Operator E2E test S10 (3 layouts back-to-back) failed
when filling slot 1 of the second layout: `wait_for_selector
slot-1-image-ok` timed out. Screenshot showed slot 1's "Choose File"
button labelled `A-9001.png` even though the React state had
`image_uploaded: false` for that slot.

**Root cause.** The Curation page rendered SlotCards with
`key={slot.idx}`. Both `grid-2x2` and `featured-row` use slots indexed
1..4. React's reconciler matched by key, kept the same SlotCard
component instance across the layout swap, and reused the underlying
`<input type=file>` DOM node — including its uncontrolled file
selection from the previous layout. React state was correctly reset
(`image_uploaded: false`), but the DOM held stale data.

**Fix.** `key={`${draft.layout_id}-${slot.idx}`}` forces React to
unmount the previous layout's SlotCards entirely so the new layout
gets fresh `<input type=file>` elements with empty file values.

**Lesson.** **Uncontrolled DOM elements (file inputs, embedded video
players, canvases) keep their internal state across React re-renders
unless you change the key.** If a component owns DOM state React
doesn't manage, key the component on whatever dimensions imply "fresh
DOM needed" — for layout swaps that's `layout_id`. The portal bug
was operator-visible (stale filenames after switching layouts), not
just an automation hazard.

### P-007 — sync_playwright reentrancy (redux) for L3 LLM calls (2026-04-28)

**Symptom.** Designing the L3 LLM heal: the runner is sync
(sync_playwright), but `pilot.agent.ai_client.complete_structured` is
async. Calling it inline from a runner method would risk a P-004-style
reentrancy deadlock if the LLM client touched any sync_playwright API.

**Root cause.** Same shape as P-004. sync_playwright dispatches its
work on a single thread/event loop; running an asyncio task inline
inside a runner method that's already executing inside that loop's
context can reenter the loop and stall.

**Fix.** `pilot.agent.locator_repair.make_repair_for_runner(client, model)`
wraps the async `complete_structured` in a sync shim that uses
`asyncio.run(...)` — fresh event loop per call, no reentrancy with
the sync_playwright loop on the runner thread. The runner thread is
itself an `asyncio.to_thread` worker, so spinning up a child loop is
bounded and safe.

**Lesson.** **The `asyncio.run` shim is the safe default for any
LLM-on-failure plumbing inside the sync runner.** Anything that
needs to call async code from inside `skill_runner.py` should go
through this pattern, not bare `await` or `loop.run_until_complete`
in the runner thread.

### P-006 — v1→v2 skill migration silently dropped all parameters (2026-04-28)

**Symptom.** Live Groq `plan` run produced a `Plan` whose first (and
only) step had `params: {}` even though the planner clearly understood
the goal — `param_sources` was fully populated, but `notes` said:
`step 1 skill curate_one_item got unknown params ['content_id', 'layout_row', 'layout_position']`.

**Root cause.** The existing `skills/curate_one_item.json` was written
under v1 (no `schema_version`, top-level field `params`). v2's
`SkillFile` schema uses `parameters`. The first cut of `upgrade_v1_to_v2`
just stamped `schema_version: 2` and called `model_validate` — Pydantic
silently produced `parameters=[]` because v2 doesn't know about the
`params` field, then `params` was rejected as an unknown extra field
or just ignored. The planner then loaded the skill with zero declared
parameters and dutifully stripped every parameter the LLM produced as
"unknown".

**Fix.** Rewrite `upgrade_v1_to_v2` in `pilot/agent/schemas/skill.py`
to perform a proper field-rename + reshape: each v1 entry
`{name, type, description, example, required}` becomes a v2
`SkillParameter(name=..., semantic=description, type=mapped, required,
default_hint=example)`. Also synthesizes a missing `id` from `name`.

**Verified.** Re-running the live Groq plan produced all 7 parameters
populated correctly; smoke + integration tests still green.

**Lesson.** **Schema migrations need both a field-rename mapping AND a
value-shape mapping.** When `model_validate` accepts the input under
the new schema with `parameters=[]` instead of raising, Pydantic isn't
"silently failing" — from its perspective the input is valid; it's the
*migration* that was wrong. Whenever a v_n+1 schema renames or
restructures a field, write the migrator to be **explicit about every
old field it cares about and every new field it produces**, then add a
test that round-trips a real v_n file and asserts the populated
field counts.

### P-005 — Groq retired some model ids without notice (2026-04-28)

**Symptom.** Initial assumption that `llama-3.3-70b-versatile` was retired,
based on a truncated `head -8` of the models endpoint.

**Root cause.** Diagnostic error on my side; `head -8` cut the model list
mid-stream. Model is in fact still live.

**Fix.** Always sort + paginate full model lists when verifying availability.

**Lesson.** Never truncate provider model lists when validating defaults.

### P-004 — `sync_playwright` reentrancy deadlock in teach (2026-04-23)

**Symptom.** Live teach captures only the initial navigate event; main
thread spins at 75% CPU; subsequent operator clicks are silent.

**Root cause.** `_on_event` called `page.screenshot()` while the main
thread was *inside* the Playwright binding callback. Sync Playwright
dispatches callbacks on its own event loop; calling sync Playwright
methods from inside the callback reenters the loop and deadlocks.

**Fix.** Decouple receipt from processing. Callback appends raw payload
to a `deque`; main pumping loop drains it where Playwright calls are
safe. (commit `1450e6f`)

**Lesson.** **Never call sync Playwright APIs from inside an `expose_binding`
callback.** This applies to any future LLM-side-channel bindings too. If
something needs to invoke Playwright in response to a binding event,
queue it and process from the main loop.

**Tooling lesson.** py-spy is invaluable for diagnosing sync Playwright
hangs. `pip install py-spy` then `py-spy dump --pid <pid>` shows the
exact stack — including whether you're inside a binding callback.

### P-003 — Event-loop starvation masquerading as "events not captured" (2026-04-23)

**Symptom.** Same as P-004 but earlier in the session, with a different
fix attempted.

**Root cause.** `start()` used `time.sleep(0.25)` which doesn't pump the
sync_playwright event loop. Binding callbacks queued forever.

**Fix.** Replace with `page.wait_for_timeout(200)` (commit `b1b0497`).
This was necessary but **not sufficient** — once it landed, callbacks
finally fired and immediately hit the P-004 reentrancy deadlock.

**Lesson.** A "fix" that resolves one symptom can expose a hidden second
bug. Always live-reproduce after a fix before declaring victory. Tests
that don't exercise the failing code path are not validation. (Our e2e
test ran with `capture_screenshots=False`, which is exactly why it never
hit P-004.)

### P-002 — Pre-existing Chrome tab missing CDP binding routing (2026-04-23)

**Symptom.** Even after P-003 fix, calling `window.__pilotCapture(...)`
in DevTools returned a Promise but Python never received the call.

**Root cause.** When Chrome is launched with the portal URL as a
command-line argument, the page exists *before* the CDP client connects.
Playwright's `expose_binding` + `add_init_script` attach to future page
loads. Existing pages get `window.__pilotCapture` as a stub, but the
underlying CDP routing isn't wired up.

**Fix.** After `expose_binding` + `add_init_script`, call
`page.reload(wait_until="domcontentloaded")` so the page picks up both
on a fresh frame. Also added a probe event (`__init_probe`) sent during
setup that must round-trip back to Python within ~1s — if it doesn't, we
raise `RuntimeError` instead of letting the operator record into the
void. (commit `b1b0497`)

**Lesson.** When attaching automation to an already-loaded page over CDP,
either reload it or never trust that pre-attach scripts will fire. A
binding probe at startup is cheap insurance against silent failure.

### P-001 — `pkill` killing the running shell (2026-04-23)

**Symptom.** Repeated `Error: Command terminated by signal: SIGKILL` when
running cleanup commands like `pkill -9 -f chrome`.

**Root cause.** `pkill -f` matches against the full command line, including
the parent shell process running the cleanup command. The cleanup script
killed itself.

**Fix.** Use `pgrep` first to inspect what would die, run cleanup in a
detached `setsid` shell, or kill specific PIDs from `ps`. Avoid broad
`pkill -f` patterns for processes that include common substrings.

**Lesson.** Always test `pgrep -f <pattern>` before `pkill -f <pattern>`.

---

## 7. Conventions `[live]`

### Commit messages

- Title: imperative present tense, lowercase first word after the type.
  Example: `Fix teach recording: decouple binding callback from Playwright calls`.
- Body: explain *why*, not *what*. Reference root cause + fix.
- Include `Co-authored-by: factory-droid[bot] <138933559+factory-droid[bot]@users.noreply.github.com>`
  on Droid-authored commits.

### Doc updates

- Every commit that changes behaviour must update `CHANGELOG.md` with
  the date, commit id, and a short paragraph.
- Major milestones (week boundaries, architecture shifts) update
  `CONTEXT.md` sections 3-5.
- Non-trivial bugs append to §6 (problems faced).

### Schema versioning

- Pydantic models for protocol payloads carry a `schema_version` field.
- Skill JSON has a top-level `version` field; readers must reject unknown.
- Portal context YAML has a top-level `schema_version` field.

### Testing

- Unit tests use the **mock AIClient** — no network calls.
- Integration tests gated behind env vars (e.g. `GROQ_API_KEY`); skipped
  cleanly if absent so CI doesn't fail when keys aren't configured.
- Live-reproduction tests are the gold standard for anything in the
  teach/replay path. Automated e2e tests are second.

### Secrets

- Never commit API keys, tokens, or session cookies.
- Tokens that appear in chat history must be revoked, even if scoped /
  short-lived.
- Test scripts that consume secrets read them from env vars only and
  redact them from any captured output.

---

## 8. Architectural decisions log `[append-only]`

> Each entry: date, decision title, what changed, why, alternatives
> considered, where it lives in `DOCS/PRODUCT_PLAN.md`. Newest at the top.
> Never delete; supersede with a new entry that explicitly references
> the prior one.

### AD-006 — Layout-as-parameter: portal-context routing now, loop-aware skills as the long-term fix (2026-04-29)

**Decision.** The current "one skill per layout" model
(`curate_carousel`, `curate_featured_row`, `curate_grid_2x2`)
doesn't match the operator's mental model — to them it's *one*
skill ("fill the curation form") with `layout` as a dropdown
parameter. Three escalating fixes; ship Option 1 immediately,
keep Option 2 on the roadmap, reject Option 3.

**Option 1 — portal-context routing (ship now).** Extend
`portals/<id>/context.yaml` with a flow-to-skill map:

```yaml
flows:
  - intent: curate
    description: "Fill the curation form for any layout"
    by_layout:
      grid-2x2: curate_grid_2x2
      featured-row: curate_featured_row
      carousel: curate_carousel
```

The planner reads this and routes goal-stated layout to the right
recorded skill. Operator's mental model preserved at the planner
layer; under the hood it's still per-layout recordings. ~30 LOC.

**Option 2 — loop-aware skills (real fix; roadmap).** Recordings
become *programs*. The annotate-LLM pass detects that steps 6-13
are four near-identical slot-fill blocks (monotonically incrementing
slot index, consistent fingerprint template), compresses them into
a single `LoopStep` with templated fingerprints
(`slot-${i}-content-select`), and the runner expands the loop at
replay based on `slot_count` from portal context. The LLM detects
the pattern at *annotate* time only — replay stays deterministic.
2-3 weeks of work; meaningful product upgrade.

**Option 3 — multi-demonstration merge.** Rejected as a halfway
house: more complex than Option 1, less general than Option 2.

**Why.** Option 1 closes the operator-experience gap immediately
without changing skill semantics. Option 2 generalizes to the
common enterprise-portal pattern of "repeated structures"
(table rows, batch forms, layouts with variable slots). Option 2
is also the natural place for portal-schema knowledge to land.

**Lives in.** `DOCS/PRODUCT_PLAN.md` §6 (V2 capabilities — added
loop-aware skills as a planned capability), `pilot/agent/planner.py`
(routing logic + portal context read), `portals/<id>/context.yaml`
(flow-to-skill maps).

### AD-005 — Planning-time coverage validation (2026-04-29)

**Decision.** Before emitting `decision="plan"`, the planner shall
confirm every `required: true` parameter on the chosen skill has
a value in `cs.params`. Missing required params -> emit clarify,
not plan. Add a defense-in-depth pre-flight check in the executor
that surfaces any leakage as `error_kind=missing_required_params`
with a structured `missing_params` list — not a stack trace.

**Why.** Today's planner validates "unknown params" (strips them)
but not "missing required params". So a plan with a 5-slot skill
filled by a 4-row CSV gets approved, runs 13 sub-steps fine, then
crashes at step 14 with `SkillExecutionError`. Operator only sees
retry/skip/abort, none of them right (P-010).

**Symmetric-validation principle.** Every check we want the LLM
to make has a deterministic post-LLM equivalent. We do this for
unknown skill ids and unknown param names; we should do it for
required-param coverage too. Asymmetric validators leak errors
into the runtime.

**Lives in.** `pilot/agent/planner.py::_validate_planner_output`
(planner-side check) + `pilot/agent/executor_real.py::execute`
(pre-flight, mirrors the existing `Path.exists()` check for
`file_path` params).

### AD-004 — Skills are recordings, not inferences (2026-04-29)

**Decision.** The planner shall not attempt to drive a flow it
has not been taught. When the operator's goal references a
layout / variant / portal section for which no recorded skill
exists, the planner emits `decision="clarify"`, not a "closest-
match" plan that substitutes a different recording.

**Why.** This is the architectural commitment that makes our
replay deterministic. The planner picking the closest skill and
hoping the recorded fingerprints "happen to work" on a different
flow is the same posture as Skyvern's vision-LLM-in-the-loop
approach — and the same source of their 14% failure rate on
write-heavy tasks per their own published benchmark.

**Where the LLM is allowed to look at the live page.** Only at
*recording* time, with the operator in the loop:
- **Annotate-LLM** sees the recorded trace and proposes semantic
  param names — operator confirms before saving.
- **Future "assisted teach" mode** could use DOM inspection to
  scaffold a teach session ("I see 5 slots in this carousel, walk
  me through filling slot 1 first"). Inference is bounded by
  operator approval; the resulting skill is auditable.

What the LLM is NOT allowed to do:
- Read the live DOM at replay time and "figure out" missing params
- Substitute a near-match skill when the requested skill doesn't exist
- Decide what an unrecorded layout / option needs without grounding
  in a portal-context schema

**Where schema-level facts live.** `portals/<id>/context.yaml` is
the right place for "carousel has 5 slots", "saving requires a
non-empty comment", and similar **portal schema facts that don't
depend on any one recording**. The planner uses this to validate
feasibility (e.g. "your CSV has 4 rows, carousel needs 5 — clarify")
even when no recording matches yet.

**Lives in.** `DOCS/requirement.md` §3.2 (deterministic execution),
§14 (portal knowledge files); `DOCS/PRODUCT_PLAN.md` §2 ("LLMs
reason at the edges; the deterministic replay core runs in the
middle"); `pilot/agent/planner.py` (skill matching).

### AD-003 — Local secrets via dotenv (2026-04-29)

**Decision.** Pilot loads a repo-root `.env` at every package
import via `python-dotenv`. `.env` is gitignored; `.env.example`
is committed as the canonical key/var template. Shell vars take
precedence (`override=False`).

**Why.** Daily operator workflow benefits from "set the key once,
forget about it". Per-shell `export GROQ_API_KEY=...` is fragile
on Windows where cmd / PowerShell / Git Bash each have different
syntax. dotenv is a single-file source of truth that works the
same in every shell.

**Security posture.** `.env.example` documents which vars exist;
`.env` is for real values. The `.env.example` includes Groq /
OpenAI / Bedrock / custom_org keys plus per-stage model overrides.
The gitignore explicitly whitelists `.env.example` so a careless
`git add .` can't commit real values.

**Lives in.** `pilot/__init__.py::_load_dotenv_if_available`,
`.gitignore`, `.env.example`, `pyproject.toml` (declares
`python-dotenv` as a core dep).

### AD-002 — v1 ships as a web app; v2 wraps it in Electron (2026-04-28)

**Decision.** Build v1's operator UI as a standalone **React 18 + TS +
Vite 5 + TailwindCSS v4 + shadcn/ui** SPA running in the operator's
regular browser, talking to a new local `pilot/agent/web_server.py`
(FastAPI). Electron is **deferred to v2** as an additive wrap of the
same React code; v2 only adds a `bridge.electron.ts` impl + Electron
main process + electron-builder.

**Why.**
- Electron's renderer *is* a Chromium-hosted React app, so anything
  written for v1's browser ships into v2 unchanged.
- v1 ships sooner — no electron-builder / signing / notarisation /
  auto-update infra on the critical path.
- Faster dev loop in browser (HMR, native devtools, no IPC ceremony).
- The desktop-only capability we actually need (driving Chromium over
  CDP) already lives in the **Python agent**, not in Electron. The
  browser UI just renders agent events.

**The seam: `HostBridge`.** Single interface in
`curationpilot-app/src/host/bridge.ts` with two implementations
(`bridge.web.ts` for v1, `bridge.electron.ts` for v2). All
browser-vs-Electron-sensitive capabilities (file picker, folder
picker, launch portal, sessions list, event subscription, command
submission) route through it. The React app must never call `fetch`
or `window.X` directly for those concerns.

**Alternatives considered.**
- *Build Electron from day one.* Rejected — pushes ship date, bakes in
  build infrastructure cost before we've validated the agent UX.
- *Tauri instead of Electron in v2.* Open option; Tauri also takes a
  React renderer unchanged, so this decision doesn't preclude it.
- *Next.js for the operator UI.* Rejected — no SSR / RSC / file-routing
  benefit for a single-window WebSocket-driven SPA; complicates the v2
  Electron port.

**Lives in.** `DOCS/PRODUCT_PLAN.md` §3.1, §3.2, §3.4, §6.3 (Week 3 +
Week 4 + V2), §11.1, §11.2.

### AD-001 — Hand-owned `AIClient` Protocol; no LiteLLM, no agent framework (2026-04-24)

**Decision.** All LLM access goes through a hand-written `AIClient`
Protocol with per-provider adapters (Bedrock, OpenAI, Groq, custom
org). Structured outputs use a provider-agnostic helper (JSON Schema
in prompt + Pydantic validation + bounded retry). The agent
orchestrator is a hand-written state machine.

**Why.** Custom org LLM has a non-standard wire format that doesn't
mesh with LiteLLM or `instructor`; agent frameworks (LangGraph,
OpenAI Agents SDK, CrewAI, Agno) all assume LLM-in-the-execution-loop,
but our executor is the deterministic runner and stays that way.

**Lives in.** `DOCS/PRODUCT_PLAN.md` §11.
