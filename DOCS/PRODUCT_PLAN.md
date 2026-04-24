# CurationPilot — Product Plan

> The design and execution plan for turning the deterministic teach/replay
> core into a conversational, agentic desktop assistant for enterprise
> portal workflows.

---

## 1. Product Vision

CurationPilot is a **desktop assistant** that performs portal workflows on
behalf of operators. The operator describes a goal in plain language
(optionally with attached files), and the assistant:

1. Understands the goal against a library of **recorded skills** and a
   **per-portal context file**.
2. Asks a small number of targeted clarification questions when truly
   needed.
3. Proposes an **auditable plan** the operator approves before any action
   runs.
4. Executes the plan by invoking deterministic recorded skills in a
   CDP-attached Chromium window the operator can watch live.
5. Pauses on failure and offers recovery options; never silently
   continues into an unknown state.
6. Produces a **post-run report** summarising what was done, with
   screenshots.

The key architectural commitment: **LLMs reason at the edges; the
deterministic replay core runs in the middle.** This preserves
auditability and reliability while still giving the product the
conversational feel of modern agentic apps.

---

## 2. Core Design Principles

1. **Deterministic core, intelligent edges.** Replay is byte-for-byte
   reproducible. LLMs contribute at intake, planning, clarification,
   locator repair (only on failure), and reporting — never on the hot
   path of a successful replay.
2. **Two mandatory human checkpoints.** Every run passes through
   clarification (if needed) and plan approval (always). Nothing
   destructive happens without both.
3. **Skills are contracts, not recordings.** A v1 skill file self-
   describes its purpose, parameters, success criteria, and destructive
   actions. The planner reasons about skills it has never seen before.
4. **Portal knowledge is authored, not guessed.** Each portal has a
   hand-authored context file describing its terminology, page map, and
   conventions. The planner is grounded against this file.
5. **Memory is earned, inspectable, and editable.** Nothing is learned
   silently. Every memory write is surfaced to the user and can be
   revised.
6. **Auditability over cleverness.** Every action, every LLM decision,
   every pause, every human input is logged to the session trace.

---

## 3. System Architecture

### 3.1 High-level topology

```
+-------------------------------------------------------------------+
|  ELECTRON DESKTOP APP                                             |
|                                                                   |
|  +----------------------------+  +-----------------------------+  |
|  | Chat / Plan Pane (React)   |  | Live Trace Pane             |  |
|  |  - chat input              |  |  - event stream             |  |
|  |  - file attach (PPT, CSV)  |  |  - screenshot thumbs        |  |
|  |  - clarify questions       |  |  - step status              |  |
|  |  - plan preview + approve  |  |  - pause / resume           |  |
|  |  - report view             |  |                             |  |
|  +----------------------------+  +-----------------------------+  |
|             |                              |                      |
|             +-------------+----------------+                      |
|                           | IPC                                   |
|             +-------------v----------------+                      |
|             | Main Process (Node.js)       |                      |
|             |  - spawns Python agent       |                      |
|             |  - spawns Chromium + CDP     |                      |
|             |  - positions Chromium window |                      |
|             +-------------+----------------+                      |
+---------------------------|---------------------------------------+
                            | stdio JSON-RPC (events + commands)
                            v
+-------------------------------------------------------------------+
|  PYTHON AGENT  (pilot/ + new agent/)                              |
|                                                                   |
|  Intake LLM   -- extracts entities from goal + attachments        |
|  Planner LLM  -- maps goal -> skill invocations                   |
|  Clarify LLM  -- generates minimal disambiguating questions       |
|  Executor     -- runs skills via deterministic replay core        |
|  Repair LLM   -- invoked only on locator fallback exhaustion      |
|  Reporter LLM -- produces post-run narrative from trace + shots   |
|                                                                   |
|  Reads: skills/*.json, portals/<name>/context.yaml                |
|  Writes: sessions/<id>/{trace.jsonl, report.md, screenshots/}     |
+---------------------------|---------------------------------------+
                            | Chrome DevTools Protocol
                            v
                 +-----------------------+
                 |  Chromium :9222       |  operator watches live
                 |  (portal in tab)      |
                 +-----------------------+
```

### 3.2 Process model

| Process | Language | Role |
|---|---|---|
| Electron main | Node.js | Window management, spawns children, IPC bridge |
| Electron renderer | React + TypeScript | UI (chat, plan, trace, report) |
| Python agent | Python 3.11 | All LLM calls + deterministic runner |
| Chromium | Native | The portal browser, user-visible |

Four processes, three protocol boundaries:

- **Renderer <-> Main**: Electron IPC (structured objects)
- **Main <-> Agent**: stdio JSON-RPC (line-delimited JSON)
- **Agent <-> Chromium**: Chrome DevTools Protocol

The agent is the same `pilot/` package that runs today. The new `agent/`
module adds the LLM wrappers and the JSON-RPC server loop. The existing
runner, adapters, locators, and teach/annotate/replay code are unchanged.

### 3.3 Attached-browser strategy

**v1: external Chromium, side-by-side windows.**

The Electron app has a "Launch portal browser" button that spawns
Chromium with `--remote-debugging-port=9222 --user-data-dir=<profile>`
and the portal URL. The operator arranges the two windows side by side
(or the app does it for them via OS-level window positioning).

**v1.1: programmatic window positioning.** On launch, the Electron app
uses `SetWindowPos` (Windows) / AppleScript (macOS) / `wmctrl` (Linux) to
snap the Chromium window to the right half of the screen.

**v2 (deferred): BrowserView embedding.** Only if user feedback demands
a single-window experience. The risk-reward does not justify it for v1.

---

## 4. Data Model

### 4.1 Skill file (intelligent v1)

```yaml
# skills/curate_one_item.yaml
id: curate_one_item
version: 2
name: "Curate one content item"
description: "Publishes a single content asset into a target slot, given
  the asset ID, destination row/position, and schedule dates."

# What the skill needs to run
parameters:
  - name: content_id
    semantic: "Asset identifier (e.g. A-9042)"
    required: true
    source_hint: "PPT slide table column 'Asset ID' or user-provided"
  - name: layout_row
    semantic: "Grid row (1-indexed)"
    required: true
  - name: schedule_start
    semantic: "Go-live date (ISO 8601)"
    required: true
    default_hint: "if absent, ask the user"

# What state the portal must be in before the skill runs
preconditions:
  - "User is logged in"
  - "Current page is the curation dashboard or any sub-page of it"

# How the skill knows it succeeded
success_assertions:
  - type: text_visible
    text: "Layout saved"
    scope: "toast"
  - type: url_matches
    pattern: "/curation"

# Destructive-action flags
destructive_actions:
  - step: 7
    kind: "save"
    reversible: true
  - step: 14
    kind: "publish"
    reversible: false
    confirm_prompt: "This will make content {content_id} live to users."

# The recorded steps (unchanged from today's format)
steps: [...]
```

Key additions over today's skill format: `description`, `parameters[].semantic`,
`parameters[].source_hint`, `preconditions`, `success_assertions`,
`destructive_actions`. These fields are populated by an LLM pass during
`annotate`, with human confirmation.

### 4.2 Portal context file

```yaml
# portals/samsung_curation/context.yaml
portal:
  id: samsung_curation
  name: "Samsung TV Plus Curation Portal"
  base_url: "https://curation.samsung.example.com"

# Domain terminology the planner needs to understand user intent
glossary:
  - term: "release"
    meaning: "A bundle of content scheduled to go live together on a
      specific date."
  - term: "slot"
    meaning: "A position in the layout grid; identified by row + position."
  - term: "curation"
    meaning: "The act of placing content into slots and scheduling it."

# High-level map of pages in the portal
page_map:
  - path: /dashboard
    role: "home"
  - path: /curation
    role: "primary workflow"
    sub_tabs: [layout, schedule, thumbnails]
  - path: /media-assets
    role: "asset library"

# How user inputs map to portal fields
field_conventions:
  content_id:
    format: "A-NNNN"
    examples: ["A-9001", "A-9042"]
  schedule_dates:
    format: "YYYY-MM-DD"
    timezone: "UTC"

# Actions that require extra caution
destructive_actions:
  - "publish"
  - "delete"
  - "archive"

# Auth / session notes
session:
  login_url: "/login"
  session_duration_minutes: 60
  sso_provider: "okta"
```

One file per portal, hand-authored, checked into the repo alongside the
skill library. The planner reads this on every task.

### 4.3 Session artifacts

Each run produces:

```
sessions/<session_id>/
  trace.jsonl           # every event (chat, plan, step, pause, resolution)
  plan.json             # the approved plan
  report.md             # post-run narrative
  screenshots/
    step_001.png
    step_002.png
    ...
```

Unchanged from today's session layout, plus `plan.json` and `report.md`.

---

## 5. JSON-RPC Event Protocol

The streaming contract between the Electron app and the Python agent.
All messages are line-delimited JSON over stdio.

### 5.1 Electron -> Agent (commands)

```json
{"type": "task.submit",
 "goal": "Curate this release and use these thumbnails",
 "attachments": [
   {"path": "/Users/.../release.pptx", "kind": "pptx"},
   {"path": "/Users/.../thumbs", "kind": "folder"}
 ]}

{"type": "clarify.answer",
 "question_id": "q1",
 "answer": "Option A"}

{"type": "plan.approve", "plan_id": "p1"}
{"type": "plan.reject", "plan_id": "p1", "reason": "wrong skill"}

{"type": "pause.resolve",
 "pause_id": "x1",
 "action": "retry" | "skip" | "abort" | "use_alternate",
 "payload": {...}}

{"type": "task.cancel"}
```

### 5.2 Agent -> Electron (streaming events)

```json
{"type": "intake.extracted",
 "entities": {
   "content_items": [{"id": "A-9001", "title": "..."}, ...],
   "dates": ["2026-05-01"],
   "files": [...]
 }}

{"type": "clarify.ask",
 "id": "q1",
 "question": "The PPT has 3 columns matching 'Asset ID'. Which one?",
 "options": [
   {"label": "Column B (Asset ID)", "value": "B"},
   {"label": "Column D (Internal Ref)", "value": "D"}
 ]}

{"type": "plan.proposed",
 "id": "p1",
 "summary": "Curate 12 items from release.pptx",
 "steps": [
   {"idx": 1, "skill": "curate_one_item",
    "params": {"content_id": "A-9001", ...},
    "param_sources": {"content_id": "pptx slide 3 cell B2"}},
   ...
 ],
 "destructive_actions": [
   {"step": 5, "kind": "publish", "items": ["A-9001", "A-9002"]}
 ],
 "estimated_duration_seconds": 45}

{"type": "step.started", "idx": 1, "skill": "curate_one_item", "params": {...}}
{"type": "step.progress", "idx": 1, "action": "click",
 "test_id": "btn-save-layout", "screenshot_path": "..."}
{"type": "step.succeeded", "idx": 1}
{"type": "step.failed",
 "idx": 1,
 "error": "locator exhausted: btn-save-layout",
 "screenshot_path": "...",
 "suggestions": [
   {"action": "retry", "label": "Retry"},
   {"action": "use_alternate",
    "label": "Use 'Save & Publish' button (LLM-suggested match)",
    "payload": {"selector": "..."}},
   {"action": "skip", "label": "Skip this step"},
   {"action": "abort", "label": "Abort run"}
 ]}

{"type": "paused",
 "id": "x1",
 "reason": "locator_failure" | "unexpected_modal" | "manual_breakpoint"}

{"type": "report.ready",
 "path": "sessions/.../report.md",
 "summary": "12 of 12 items curated successfully",
 "warnings": ["Item A-9007 saved with a non-standard schedule date"]}

{"type": "agent.log", "level": "info" | "warn" | "error", "message": "..."}
```

Locking this protocol in week 1 is the single most important
deliverable — it's the contract the UI and the agent evolve against
independently.

---

## 6. V1 Plan — Minimum Shippable Product

### 6.1 Scope (in)

- Chat-driven goal input (no voice)
- File attachments (PPT, CSV, folder)
- Intake + planner + clarify LLM pipeline
- Intelligent skill files (description, parameters, success assertions,
  destructive flags) generated in `annotate` with human confirmation
- Portal context file (hand-authored, one per portal)
- Plan preview + explicit approval step
- Execution via existing deterministic runner
- Basic mid-loop pause on step failure with {retry, skip, abort} options
- Post-run report (LLM-generated from trace + screenshots)
- Electron desktop shell: chat pane + live trace pane
- "Launch portal browser" button spawning Chromium with CDP
- Session history (list past runs, reopen their reports)

### 6.2 Scope (out — deferred to v2)

- Cross-session memory / auto-research / learning
- Voice input
- Rich mid-loop conversational recovery (v1 has basic pause only)
- LLM-driven locator repair suggestions (v1 aborts on fallback exhaustion)
- Natural-language one-shot tasks (no recorded skill)
- Window embedding / single-window experience
- Multi-user / team features
- Skill authoring UI (v1: operators still use the CLI to teach)

### 6.3 V1 execution plan (4 weeks)

#### Week 1 — Agent backbone, no UI

**Deliverables:**
- [x] `pilot/agent/ai_client/` — AIClient Protocol + structured-output
      helper + registry + Bedrock / OpenAI / Groq adapters + custom_org
      skeleton (already landed before week 1 started)
- [ ] JSON-RPC event protocol documented in `DOCS/PROTOCOL.md`
- [ ] `pilot/agent/intake.py` — extracts entities from goal + attachments
- [ ] `pilot/agent/planner.py` — maps goal + entities -> plan
- [ ] `pilot/agent/clarify.py` — generates minimal disambiguating questions
- [ ] `pilot/agent/orchestrator.py` — hand-written state machine
- [ ] `pilot/agent/server.py` — stdio JSON-RPC loop wrapping the above
- [ ] CLI entry: `pilot agent do "goal" --attach file.pptx` that prints
      events as JSONL
- [ ] Portal context file for the sample portal (hand-authored)
- [ ] Per-stage model eval harness: run each stage's prompt against
      candidate models, pick empirical defaults
- [ ] Prompt-test harness: sample goals + expected plans, run as CI

**Exit gate:** operator can type a goal on the CLI, see clarify questions,
answer them, see a plan, approve, and the plan executes using today's
deterministic runner. No UI yet.

#### Week 2 — Intelligent skills + plan preview + mid-loop pause

**Deliverables:**
- [ ] `pilot/agent/annotate_llm.py` — LLM pass that enriches skill files
      with `description`, `parameters[].semantic`, `success_assertions`,
      `destructive_actions`
- [ ] Updated `annotate` CLI: shows LLM proposals, operator confirms /
      edits in a terminal wizard
- [ ] Skill schema v2 documented; migration from v1 skills is automatic
- [ ] Plan preview format (structured, auditable); approval prompt in CLI
- [ ] Mid-loop pause: on step failure the runner emits `step.failed`,
      waits on stdin for `pause.resolve`
- [ ] Destructive-action confirmation: every destructive step requires
      a per-step "yes" unless pre-approved in the plan

**Exit gate:** a realistic workflow (curate 3 items from a PPT) runs
end-to-end in CLI with intelligent-skill metadata driving the planner's
decisions.

#### Week 3 — Electron shell

**Deliverables:**
- [ ] Electron app scaffold (TypeScript + React)
- [ ] Main process spawns Python agent subprocess, bridges JSON-RPC
      over stdio to renderer via IPC
- [ ] Main process "Launch portal browser" action spawns Chromium
      with CDP; window positioning on Windows + macOS
- [ ] Chat pane: message history, input box, file attach via drag/drop
- [ ] Clarify question UI: renders `clarify.ask` events as option
      buttons (multi-select, with custom-answer escape hatch)
- [ ] Plan preview UI: structured render of `plan.proposed`, with
      destructive steps in red; Approve / Reject buttons
- [ ] Live trace pane: step list, current step highlight, screenshot
      thumbnails per step
- [ ] Mid-loop pause UI: modal with the suggestion list from
      `step.failed`, buttons wired to `pause.resolve`

**Exit gate:** operator can run the same week 2 workflow entirely from
the Electron app. No terminal needed.

#### Week 4 — Reporter + polish + dogfood

**Deliverables:**
- [ ] `pilot/agent/reporter.py` — reads trace + screenshots,
      produces narrative markdown report
- [ ] Report view in Electron app (render markdown, expand screenshots)
- [ ] Session history view: list of past runs with status + timestamp
- [ ] Crash recovery: if agent dies mid-run, app reconnects to the
      most recent session and shows state
- [ ] Dogfood on 3-5 real workflows; triage every failure
- [ ] v1 release: signed installer for Windows, notarised for macOS

**Exit gate:** three external operators complete their real workflows
without intervention from the dev team.

### 6.4 V1 risks and mitigations

| Risk | Mitigation |
|---|---|
| Chromium window positioning is flaky on some OSes | v1.0 ships with manual window arrangement; programmatic positioning is v1.1 |
| LLM-generated plans misidentify skills | Plan preview is always shown; nothing runs without approval |
| LLM-generated skill metadata is wrong | Human confirmation required during annotate; metadata shown in plan preview with its source tagged |
| Python <-> Node stdio deadlocks | Line-delimited JSON, flush after every message, 30s liveness heartbeat |
| Portal session expires mid-run | Detect via assertion + pause with "please log back in" option |
| User goals are too vague to plan | Clarify loop with hard budget (5 questions); if still ambiguous, planner emits a "cannot disambiguate" event |

---

## 7. V2 Plan — After V1 Is Shipped and Dogfooded

### 7.1 Themes

1. **Learning from use.** The agent becomes faster and smarter the more
   an operator uses it.
2. **Resilience.** Fewer hard stops, more graceful recoveries.
3. **Breadth.** Beyond recorded skills — natural-language one-shot tasks.
4. **Input modalities.** Voice, richer attachments.

### 7.2 Feature list

- **Portal memory (auto-research).** After a task, the agent asks:
  "Should I remember that content_id always comes from column B of this
  kind of PPT?" Explicit consent; stored in `portals/<name>/memory.yaml`;
  editable in-app; expiring after N months unless reinforced.
- **Skill memory.** Per-skill resolved patterns. "For `curate_one_item`,
  schedule_start is typically the Monday of next week."
- **LLM locator repair.** When the 4-level fallback exhausts, the Repair
  LLM inspects the current DOM + fingerprint + screenshot and proposes a
  semantic match. Operator confirms once; mapping is cached in the skill.
- **Rich mid-loop conversation.** Instead of {retry, skip, abort} the
  agent offers "I see an unexpected modal saying 'Session expiring'.
  I can dismiss it and continue. Proceed?"
- **Natural-language one-shot.** "Mark all pending items older than 7
  days as archived." No recorded skill; planner decomposes into portal-
  ontology primitives (from the portal map) + skill-library calls.
- **Voice input.** Push-to-talk in the chat pane. Whisper-small locally
  if privacy-sensitive, OpenAI/etc. otherwise.
- **Skill authoring UI.** Guided teach mode inside the Electron app:
  step through the recording, edit params, confirm success assertions
  without touching the CLI.
- **Team features.** Shared skill library, shared portal context,
  per-user memory, per-team audit views.
- **Cost / latency dashboard.** Token usage per task, latency breakdown,
  which LLM calls are slow.

### 7.3 V2 is not a fixed plan

It's a menu. Pick from it based on what v1 users actually ask for.
Premature commitment to v2 features before v1 data is available is the
same mistake as premature memory — wrong priorities, rewrites later.

---

## 8. User Journey

### 8.1 First-time setup (one-time, ~10 minutes)

1. Operator installs the Electron app (signed installer).
2. On first launch, a setup wizard asks:
   - Portal URL
   - Path to a portal context file (or "I don't have one — use the
     starter template")
   - Chrome / Chromium binary location (auto-detected)
   - LLM provider + API key (OpenAI / Anthropic / local)
3. Operator clicks "Launch portal browser" once; Chromium opens,
   operator logs in, closes the setup wizard.
4. Operator runs `pilot teach onboarding_smoke_test --base-url ...` from
   the built-in terminal pane (or uses the guided teach UI in v2) to
   record one sample skill and verify the loop end-to-end.

### 8.2 Daily use — a typical task

**Scenario.** The operator receives `release.pptx` listing 12 content
items to be curated for next week's release, plus a folder `thumbs/`
with thumbnail images matching each item.

1. **Open the app.** Chat pane on the left is empty, trace pane on the
   right is empty, status shows "Portal browser: not launched".
2. **Launch the portal browser.** Operator clicks "Launch portal
   browser". Chromium opens to the curation portal login page. They log
   in once (session cookie persists via the dedicated user-data-dir).
3. **Describe the goal.** In the chat pane:
   > Curate this release and use these thumbnails.
   > [attaches release.pptx, drags in thumbs/ folder]
4. **Intake feedback.** The app streams back:
   > Found 12 content items in release.pptx (column B). Matched 11
   > thumbnails in thumbs/; one is missing (A-9007).
5. **Clarify.** One question appears:
   > For items without thumbnails, should I skip the thumbnail step
   > or stop and ask?
   > [Skip thumbnail step] [Stop and ask me per item]
   Operator picks "Stop and ask me per item".
6. **Plan preview.** A structured plan appears:
   > Plan (p1): Curate 12 items from release.pptx
   > Steps: 12 x `curate_one_item` (with params per item)
   > Destructive actions: 12 x publish (reversible: false)
   > Estimated duration: ~6 minutes
   > [Approve] [Reject and refine]
   Operator reviews the parameter table, sees `content_id` comes from
   "pptx slide N column B" for each item, clicks **Approve**.
7. **Execution.** The trace pane comes alive:
   > Step 1/12 — Curate A-9001 — running
   > [click nav-curation] [fill layout form] [click save] ...
   > Step 1/12 — complete
   > Step 2/12 — Curate A-9002 — running
   >
   The operator watches the Chromium window on the right.
8. **Mid-loop pause.** On item 7 (the one without a thumbnail):
   > Paused: this item has no matching thumbnail.
   > [Skip thumbnail step for A-9007] [Abort run]
   Operator clicks "Skip thumbnail step for A-9007".
9. **Completion.** After all 12 items:
   > Run complete. 12/12 items curated. 1 warning (A-9007 published
   > without thumbnail).
   > [View report]
10. **Report.** The report view shows a markdown summary with per-item
    status, embedded screenshots for key steps, and the warnings list.

### 8.3 Recovery flow — something breaks

Same scenario, but on item 4 the "Save Layout" button has been renamed
to "Save & Continue" in a portal release last night.

- Deterministic 4-level fallback exhausts (test_id gone, accessible name
  mismatch, role-based match ambiguous, text match fails).
- Runner emits `step.failed` with `{error: "locator exhausted",
  screenshot: ..., suggestions: [retry, skip, abort]}`.
- Electron app shows a pause modal with the screenshot.
- In v1: operator clicks "Abort run", manually fixes the skill, reruns.
- In v2 (with LLM locator repair): a fourth option appears —
  "Use 'Save & Continue' button (LLM-suggested match, 94% confidence)".
  Operator clicks it; run continues; mapping is cached in the skill.

---

## 9. Visual Layout

### 9.1 Main window (3-pane layout)

```
+------------------------------------------------------------------+
|  CurationPilot                                        [ ? ] [ _ ]|
+------------------------------------------------------------------+
|                                                                  |
|  +----------------------+  +--------------------------------+   |
|  | CHAT / PLAN          |  | LIVE TRACE                     |   |
|  |                      |  |                                |   |
|  |  [ Agent messages,   |  |  Step 3 / 12                   |   |
|  |    clarify cards,    |  |  -----------------------------  |   |
|  |    plan preview,     |  |  (o) Step 1  A-9001   done     |   |
|  |    report view ]     |  |  (o) Step 2  A-9002   done     |   |
|  |                      |  |  (>) Step 3  A-9003   running  |   |
|  |                      |  |        click btn-save-layout   |   |
|  |                      |  |        [screenshot thumbnail]  |   |
|  |                      |  |  (.) Step 4  A-9004   queued   |   |
|  |                      |  |  (.) Step 5  A-9005   queued   |   |
|  |                      |  |  ...                            |   |
|  |                      |  |                                |   |
|  |                      |  |  [Pause] [Cancel run]          |   |
|  |                      |  |                                |   |
|  | +------------------+ |  |                                |   |
|  | | Type a goal...   | |  |                                |   |
|  | | [attach] [send]  | |  |                                |   |
|  | +------------------+ |  |                                |   |
|  +----------------------+  +--------------------------------+   |
|                                                                  |
|  Portal browser: Running (pid 29358) [Focus] [Restart] [Close]  |
+------------------------------------------------------------------+
```

The Chromium portal browser is a **separate OS window** on the right
half of the screen (auto-positioned on launch). The operator can
rearrange at will.

### 9.2 Clarify card (in chat pane)

```
+------------------------------------------------------------+
|  Clarification needed                                      |
|                                                            |
|  The PPT has 3 columns that might be the asset ID.         |
|  Which one should I use?                                   |
|                                                            |
|  ( ) Column B - "Asset ID"           [12 values, all A-N..]|
|  ( ) Column D - "Internal Ref"       [12 values, mixed]    |
|  ( ) Column F - "External Code"      [8 values, 4 blank]   |
|                                                            |
|  [Confirm selection]        [Type a different answer...]   |
+------------------------------------------------------------+
```

### 9.3 Plan preview (in chat pane)

```
+------------------------------------------------------------+
|  Plan proposed                                             |
|                                                            |
|  Summary: Curate 12 items from release.pptx                |
|  Skill:   curate_one_item (x12)                            |
|  Duration: ~6 min                                          |
|                                                            |
|  Parameters (showing first 3 of 12):                       |
|    1. content_id=A-9001  row=1  pos=1  start=2026-05-01   |
|    2. content_id=A-9002  row=1  pos=2  start=2026-05-01   |
|    3. content_id=A-9003  row=1  pos=3  start=2026-05-01   |
|    [show all 12...]                                        |
|                                                            |
|  !! Destructive actions (12):                              |
|    - publish A-9001 ... A-9012 (not reversible)            |
|                                                            |
|  [Approve and run]   [Reject]   [Edit parameters...]       |
+------------------------------------------------------------+
```

### 9.4 Mid-loop pause (modal over main window)

```
+------------------------------------------------------------+
|  Step failed                                               |
|                                                            |
|  Step 7/12: Curate A-9007                                  |
|  Error: no matching thumbnail file for A-9007              |
|                                                            |
|  [screenshot of current portal state]                      |
|                                                            |
|  How do you want to proceed?                               |
|                                                            |
|    ( ) Skip the thumbnail step for this item               |
|    ( ) Abort the run                                       |
|    ( ) Retry (if you think this was transient)             |
|                                                            |
|  [Apply and continue]                                      |
+------------------------------------------------------------+
```

### 9.5 Report view (replaces chat pane on request)

```
+------------------------------------------------------------+
|  Report - Session abc123 - 2026-04-23 14:05                |
|                                                            |
|  # Summary                                                 |
|  12 items curated. 11 succeeded fully, 1 with a warning.   |
|                                                            |
|  # Per-item results                                        |
|  - A-9001  ok   (3.2s)                                     |
|  - A-9002  ok   (3.1s)                                     |
|  - A-9003  ok   (3.4s)                                     |
|    ...                                                     |
|  - A-9007  warn "skipped thumbnail" (2.9s)                 |
|    ...                                                     |
|                                                            |
|  # Warnings                                                |
|  - A-9007 published without thumbnail                      |
|                                                            |
|  # Screenshots                                             |
|  [thumbnail row, click to expand]                          |
|                                                            |
|  [Export markdown]  [Copy link]  [Back to chat]            |
+------------------------------------------------------------+
```

---

## 10. Capabilities and Features

### 10.1 V1 capabilities

- [F] **Natural-language goal input** with file attachments (PPT, CSV,
      folder, single image, PDF)
- [F] **Entity extraction** from attachments (content IDs, dates,
      table columns)
- [F] **Minimal clarify loop** with hard question budget
- [F] **Auditable plan preview** with per-parameter source attribution
- [F] **Explicit plan approval** (no action without it)
- [F] **Destructive-action highlighting** in plan preview
- [F] **Deterministic replay** via existing runner (unchanged)
- [F] **Live trace view** with per-step screenshots
- [F] **Mid-loop pause on failure** with retry / skip / abort
- [F] **Post-run report** generated by LLM from trace + screenshots
- [F] **Session history** browser
- [F] **Attached portal browser** (Chromium + CDP, side-by-side)
- [F] **Session persistence** across app restarts
- [F] **Crash recovery** — reconnect to the most recent session

### 10.2 V2 capabilities (planned)

- [ ] **Cross-session memory** with user consent and inspection
- [ ] **LLM locator repair** on fallback exhaustion
- [ ] **Rich mid-loop conversation** (beyond retry/skip/abort)
- [ ] **Natural-language one-shot tasks** without recorded skills
- [ ] **Voice input**
- [ ] **Guided teach UI** inside the app (no CLI required)
- [ ] **Skill drift detection** (proactive maintenance)
- [ ] **Team / shared skill library**
- [ ] **Cost & latency dashboard**
- [ ] **Programmatic window embedding** (single-window experience)

### 10.3 Non-features (explicitly not planned)

- Mobile app
- Support for portals that don't have a stable DOM (canvas-based UIs)
- Fully autonomous operation (no human approval) — this is a design
  non-goal; the product's value proposition is the approval gate
- Agent-to-agent collaboration
- Multi-tab / multi-portal workflows in a single task (v1 assumes one
  portal per task)

---

## 11. Tech Stack & Framework Decisions

This section captures the load-bearing technology choices and the
reasoning behind them. The overall posture is: **prefer thin, boring,
owned code over trendy frameworks, especially for pieces that need to
be auditable and stable for years.**

### 11.1 Adopt

| Concern | Choice | Rationale |
|---|---|---|
| LLM provider abstraction | Hand-owned `AIClient` Protocol + per-provider adapters | Custom org LLM has a non-standard format; a neutral owned interface is cleaner than a dependency that has to support dozens of providers |
| Structured LLM outputs | JSON Schema in prompt + Pydantic validation + bounded retry | Works across every adapter including ones with non-standard formats; no framework dependency |
| Prompt templates | Jinja2 files in `pilot/agent/prompts/` | Version-controlled, diffable |
| LLM retries / backoff | `tenacity` | Unremarkable, minimal |
| Agent orchestrator | Hand-written ~200 LOC state machine | Our flow (intake → clarify → plan → approve → execute → report) is linear with one checkpoint loop; frameworks impose more structure than we need |
| JSON-RPC server (Python) | stdlib asyncio + stdin/stdout, line-delimited JSON | Protocol is the spec; no framework required |
| Observability | `logfire` (optional), session trace | Every LLM call carries latency + usage from the AIClient layer |
| Electron shell | Electron 30+ | Standard desktop app platform |
| UI framework | React + TypeScript + Tailwind + shadcn/ui | Boring, widely-used, works in Electron |
| Streaming chat UI | Vercel AI SDK (`ai` + `@ai-sdk/react`) | Best-in-class streaming primitives in React; no reason to roll our own |
| State management (UI) | Zustand | Small, fast, no Redux ceremony |
| IPC renderer↔main | Electron's `ipcRenderer` + contextBridge | Built-in; vanilla |

### 11.2 Explicitly reject

| Not using | Why |
|---|---|
| **LiteLLM** | Historical vulnerability surface in the proxy component + large dependency tree + doesn't cleanly handle custom-format LLMs; our three-adapter approach gives full audit trail of what's sent where |
| **OpenAI Agents SDK, LangGraph, CrewAI, Agno** | All assume LLM-in-the-execution-loop; our executor is the deterministic `runner.py` and stays that way. Frameworks would force glue code to un-frame them |
| **A2A (Google Agent2Agent protocol)** | Solves inter-agent discovery across vendors — a problem we don't have in v1 |
| **MCP (Model Context Protocol)** | Worth tracking for v2 (could expose our pilot runner as an MCP server so Claude Desktop / Cursor can trigger portal workflows) but not a v1 foundation |
| **LangChain** | Huge surface area, frequent breakage, not needed when our LLM use is 5-6 well-specified calls |
| **`instructor`** | Wraps specific providers; doesn't mesh cleanly with custom-format org LLM. We use a provider-agnostic structured-output helper instead |

### 11.3 The `AIClient` layer in detail

All LLM access goes through one Protocol:

```python
# pilot/agent/ai_client/base.py
class AIClient(Protocol):
    name: str                 # "bedrock", "groq", "openai", "custom_org"
    default_model: str | None

    async def complete(
        self, messages, *, model=None, temperature=0.0,
        max_tokens=None, tools=None, stop=None, timeout_s=None,
    ) -> Completion: ...

    async def stream(...) -> AsyncIterator[StreamChunk]: ...

    async def close(self) -> None: ...
```

Supporting types:
- `Message(role, content, name, tool_call_id, tool_calls)`
- `ToolDef(name, description, parameters_schema)`
- `ToolCall(id, name, arguments)`
- `Completion(text, tool_calls, finish_reason, model, usage, latency_ms, provider, raw)`
- `StreamChunk(text_delta | tool_call | done, finish_reason, usage)`
- `Usage(prompt_tokens, completion_tokens, total_tokens)`

**Structured outputs** are provider-agnostic:

```python
# pilot/agent/ai_client/structured.py
async def complete_structured(
    client: AIClient,
    messages: list[Message],
    *,
    response_model: type[BaseModel],
    model: str | None = None,
    temperature: float = 0.0,
    max_retries: int = 2,
) -> BaseModel:
    # 1. Inject JSON Schema into system prompt
    # 2. Call client.complete()
    # 3. Extract JSON blob (tolerates code fences and prose)
    # 4. Validate via Pydantic
    # 5. On failure: append bad response + correction instruction,
    #    retry up to max_retries times
    ...
```

This helper is the ONLY way the rest of the agent asks for structured
LLM output. No adapter needs its own structured-output implementation.

### 11.4 Adapter modules

Located in `pilot/agent/ai_client/adapters/`. Each adapter registers
itself at import time via `register_client(name, factory)`. Adapters
are loaded lazily by the registry, so a missing provider SDK only
breaks that adapter, not the whole agent.

v1 ships with:

- **bedrock.py** — AWS Bedrock via the Converse / ConverseStream APIs
  (unified chat interface across all Bedrock-hosted model families:
  Claude, Llama, Nova, Mistral). Requires `boto3`.
- **openai.py** — Official `openai` async SDK. Works against
  api.openai.com or any OpenAI-compatible endpoint (`OPENAI_BASE_URL`).
  Requires `openai`.
- **groq.py** — Official `groq` async SDK (OpenAI-compatible shape,
  shares the `_openai_compat.py` translation helpers with the OpenAI
  adapter). Requires `groq`.
- **custom_org.py** — Template / skeleton for your organisation's
  internal LLM. No third-party deps. Registration is commented out
  until the real client is dropped in; once `register_client("custom_org", _factory)`
  is enabled, `get_client("custom_org")` works.

Shared translation helpers for OpenAI-compatible APIs live in
`adapters/_openai_compat.py` (messages → OpenAI format, tools → OpenAI
format, response → `Completion`, stream → `AsyncIterator[StreamChunk]`).

### 11.5 Per-stage model routing

Different pipeline stages benefit from different models. The config
surface exposes this directly so swapping is a config change, not a
code change:

```yaml
# ~/.curationpilot/config.yaml
ai:
  default_client: custom_org
  stages:
    intake:     { client: groq,       model: llama-3.3-70b-versatile }
    planner:    { client: bedrock,    model: anthropic.claude-sonnet-4-20250929-v1:0 }
    clarify:    { client: custom_org, model: mycompany-v2 }
    reporter:   { client: groq,       model: llama-3.3-70b-versatile }
    repair:     { client: bedrock,    model: anthropic.claude-sonnet-4-20250929-v1:0 }
```

Rule of thumb: planner and repair (v2) get the smartest model; intake,
clarify, and reporter can use a faster/cheaper one. Week 1 includes a
side-by-side eval of each stage's prompt across candidate models so the
defaults are empirical, not guessed.

### 11.6 Security posture

Owning the client layer removes a non-trivial security surface:

- No runtime behavior changes from silent upstream updates
- No surprise telemetry
- Full audit trail of exactly what is sent to which endpoint
- Per-provider auth and timeout policy under our control
- Org security review happens on our code once, not on a third-party
  library every update

For an enterprise product that handles session cookies, PII in form
fields, and possibly regulated data, this is the right posture.

### 11.7 Risk to monitor

If the custom org LLM is meaningfully weaker than frontier models on
structured output and reasoning, planner quality suffers. Mitigation:
the per-stage router in §11.5 lets us route the planner to Bedrock
Claude while using the org model for intake / clarify / reporter.
Measurement is part of the week 1 eval.

---

## 12. Open Questions

These are flagged for discussion during week 1 before lockin.

1. **Default LLM provider.** Custom org LLM, Bedrock, or Groq? Per-stage
   override is already supported; this is only about the ``default_client``.
2. **Portal context authoring.** Who writes the portal context file —
   the operator themselves, or a power user / admin? Do we need an
   in-app authoring wizard in v1?
3. **Skill library location.** Per-user local, or synced via Git / a
   shared drive? v1 default is local; v2 adds sharing.
4. **Plan cost estimation.** Should the plan preview show an estimated
   LLM token cost alongside the duration estimate? (Useful for
   budget-conscious teams.)
5. **Offline mode.** What can the app do without an LLM connection?
   v1 proposal: deterministic replay of an already-approved plan works
   offline; everything else requires the LLM.
6. **Portal context file format.** YAML vs JSON Schema vs Markdown with
   structured sections? Proposal: YAML for human authoring, validated
   against a JSON Schema.

---

## 13. Appendix

### 13.1 Directory layout at v1

```
Portal-assistant-bot/
  pilot/
    __init__.py
    runner.py
    teach.py
    annotate.py
    replay.py
    adapters/
    agent/                         <-- new in v1
      __init__.py
      intake.py
      planner.py
      clarify.py
      annotate_llm.py
      reporter.py
      orchestrator.py              <-- hand-written state machine
      server.py                    <-- JSON-RPC stdio loop
      prompts/
        intake.jinja
        planner.jinja
        clarify.jinja
        reporter.jinja
      ai_client/                   <-- provider abstraction
        __init__.py
        base.py                    <-- AIClient Protocol + types
        registry.py                <-- get_client / register_client
        structured.py              <-- complete_structured helper
        adapters/
          __init__.py
          _openai_compat.py        <-- shared translation for OAI-compat
          bedrock.py
          openai.py
          groq.py
          custom_org.py            <-- skeleton for org LLM
  app/                             <-- new in v1
    package.json
    electron/
      main.ts
      preload.ts
    renderer/
      src/
        App.tsx
        panes/
          ChatPane.tsx
          TracePane.tsx
          ReportPane.tsx
        components/
          ClarifyCard.tsx
          PlanPreview.tsx
          PauseModal.tsx
    build/
  portals/                         <-- new in v1
    samsung_curation/
      context.yaml
  skills/
    curate_one_item.yaml           <-- upgraded to schema v2
  sessions/
  sample_portal/
  DOCS/
    PRODUCT_PLAN.md                <-- this file
    PROTOCOL.md                    <-- new in v1
    GUIDE.md
    requirement.md
```

### 13.2 Terminology

- **Skill** — a recorded + annotated portal workflow, parameterised,
  stored as YAML/JSON in `skills/`.
- **Portal context** — hand-authored knowledge file about a specific
  portal, stored in `portals/<name>/context.yaml`.
- **Task** — one end-to-end run: goal -> clarify -> plan -> approve ->
  execute -> report.
- **Session** — the persistent record of one task, stored in
  `sessions/<id>/`.
- **Deterministic runner** — the existing `pilot` replay core. Does
  not call LLMs. Byte-for-byte reproducible.
- **Agent** — the new `pilot/agent/` module that wraps LLM calls and
  exposes a JSON-RPC interface.
- **AIClient** — Protocol in `pilot.agent.ai_client.base` that every
  provider adapter implements. The agent orchestrator only talks to
  `AIClient`, never to a vendor SDK directly.
- **Adapter** — concrete implementation of `AIClient` for one provider
  (Bedrock, Groq, OpenAI, or the organisation's internal LLM).

### 13.3 Related documents

- `DOCS/requirement.md` — original product requirements
- `DOCS/GUIDE.md` — operator guide for teach / annotate / replay
- `DOCS/PROTOCOL.md` — JSON-RPC event protocol (to be written in week 1)
