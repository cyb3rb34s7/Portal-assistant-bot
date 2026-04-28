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

Approach (held throughout):
- Pydantic schemas for every LLM input/output and every JSON-RPC payload.
- Mock AIClient for unit tests; real Groq for integration tests, gated
  behind `GROQ_API_KEY`.
- Hand-written orchestrator (~430 LOC). No agent framework.

---

## 5. Next up `[live]`

After Week 1 ships:

**Week 2** — intelligent-skill metadata pass during `annotate`, plan-
preview UX in CLI, mid-loop pause hardening, dogfood on real workflows.

**Week 3** — Electron shell (chat + plan + trace pane), spawns Chromium
with CDP via button, JSON-RPC over stdio bridges agent ↔ renderer.

**Week 4** — Reporter LLM, session history, crash recovery, signed
installer for Windows + macOS notarisation.

V2 deferred features list lives in `DOCS/PRODUCT_PLAN.md` §7.

---

## 6. Problems faced & solutions `[append-only]`

> **Rule.** When a non-trivial bug is found, document it here with the
> root cause + fix + commit id. Never delete entries; future-you will
> need them. New entries go at the top.

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
