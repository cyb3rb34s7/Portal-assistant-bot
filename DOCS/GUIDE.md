# CurationPilot POC — Feature Guide and Runbook

> This is the operator + developer guide for the CurationPilot proof of
> concept. It describes what the tool does today, how the pieces fit
> together, and the exact commands to use every feature.
>
> Status as of the current iteration: the teach → annotate → replay loop
> works end to end on the bundled sample portal, with automated drift
> resilience via a four-level locator fallback.

---

## 1. What CurationPilot is

CurationPilot is a local, supervised browser automation tool that **learns
a portal task by watching an operator perform it once**, then **replays
that task with different inputs** against the same portal.

Three phases:

```
  TEACH                 ANNOTATE                    REPLAY
  -----                 --------                    ------
  Operator uses the     Raw trace is filtered,      Skill runs against
  portal normally.      labelled, and turned        the live portal with
  Every click, fill,    into a reusable, named,     operator-supplied
  navigation is         parameterised Skill.        parameters. Four-level
  captured silently     Auto or interactive.        locator fallback
  with a rich element                               handles drift.
  fingerprint.
```

The capture is **silent**. No overlay, no toolbar, no visible UI is
injected into the portal page. The operator just uses the portal. The
Python-side CLI shows a live event stream in the terminal.

---

## 2. Architecture at a glance

```
OPERATOR'S MACHINE
│
├── Chrome (real, logged-in session)
│     ├── Portal tab (any framework: React, Vue, Angular, plain HTML)
│     │     └── pilot/overlay/grabber.js   (injected at page load,
│     │                                     captures DOM events, no UI)
│     │          │
│     │          └── window.__pilotCapture(payload)  (Runtime.addBinding)
│     │                    │
│     └── CDP :9222 ───────┘
│                 │
│                 ▼
├── Python pilot process
│     ├── pilot.teach       -> recorder (writes sessions/<id>/trace.jsonl)
│     ├── pilot.annotate    -> trace -> skills/<name>.json
│     ├── pilot.skill_runner-> skill + params -> replayed actions
│     ├── pilot.audit       -> JSONL audit log + screenshots per step
│     └── pilot.cli         -> Typer CLI entry points
│
└── sample_portal/         (React + Vite demo portal used for dev & tests)
```

Key points:

- The pilot **never launches or closes Chrome**. It attaches to the
  operator's Chrome over the Chrome DevTools Protocol (CDP).
- The injected grabber runs in the **main world** of the page. Works with
  any frontend framework. No React/Vue/Angular-specific code.
- Communication from JS back to Python uses `Runtime.addBinding`, so no
  WebSocket, no local HTTP server, no CORS setup.

---

## 3. Prerequisites

| Thing | Version |
|---|---|
| Operating system | Windows 10/11 (primary). macOS/Linux works with path edits. |
| Python | 3.10 or later |
| Node.js | 18 or later |
| Google Chrome | any recent version |
| Git Bash or PowerShell | for running commands |

Disk footprint for a single session run is ~1 MB (screenshots + JSONL).

---

## 4. One-time setup

From the project root:

```bash
# Python side
py -m venv .venv
.venv/Scripts/python.exe -m pip install --upgrade pip
.venv/Scripts/python.exe -m pip install -e .

# Sample portal side
cd sample_portal && npm install && cd ..
```

Verify:

```bash
# Should show the pilot CLI commands
.venv/Scripts/python.exe -m pilot --help
```

Expected output includes: `run`, `doctor`, `teach`, `annotate`, `run-skill`.

---

## 5. The three-phase workflow

You'll run each phase against the same operator Chrome instance. Keep
the Chrome window open across all phases.

### 5.1 Launch the portal and Chrome (ongoing, two terminals)

**Terminal A — sample portal:**

```bash
cd sample_portal
npm run dev
# Leave running. Portal listens on http://localhost:5173
```

**Terminal B — Chrome with CDP enabled:**

```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$USERPROFILE/.curationpilot-chrome-profile" \
  --no-first-run --no-default-browser-check \
  http://localhost:5173
# Leave running. A Chrome window opens on the portal.
```

Sanity-check the connection in a third terminal:

```bash
.venv/Scripts/python.exe -m pilot doctor
# Prints: Connected. N page(s): - http://localhost:5173/
```

### 5.2 TEACH — record a skill

```bash
.venv/Scripts/python.exe -m pilot teach my_first_skill
```

The terminal prints a banner and starts a live event stream. **Go to
the Chrome window and use the portal normally.** Click things, fill
forms, switch tabs, navigate pages — do whatever the task requires.

Every meaningful action appears in the terminal:

```
#004 click          btn-save-layout
#005 input_change   input-layout-content-id   A-9001
#006 click          curation-tab-schedule
```

When done, press **Ctrl+C** in the terminal. A summary table and a
session id print at the end. Artifacts land in `sessions/<session-id>/`.

### 5.3 ANNOTATE — turn the trace into a skill

**Auto mode** (recommended to start — applies heuristic defaults and
saves without prompting):

```bash
.venv/Scripts/python.exe -m pilot annotate <session-id> --auto
```

**Interactive mode** (walk through every step, label it, mark params
and gates):

```bash
.venv/Scripts/python.exe -m pilot annotate <session-id>
```

At each step you can:

- **keep / skip / quit** — skip removes noise events
- **label** — rename the step (default is auto-derived from the target's
  testid/aria-name/text)
- **parameter binding** — mark a filled value as a parameter so replay
  can supply a different value (e.g. `content_id`)
- **gate flag** — mark the step as irreversible so replay pauses for
  operator approval before executing it

Output is written to `skills/<skill-name>.json`.

### 5.4 REPLAY — run the skill with new parameters

```bash
.venv/Scripts/python.exe -m pilot run-skill skills/my_first_skill.json \
  -p content_id=A-9042 \
  -p start_date=2026-06-01 \
  -p end_date=2026-06-30
```

Parameters can also be loaded from a JSON file:

```bash
.venv/Scripts/python.exe -m pilot run-skill skills/my_first_skill.json \
  --params-file params/june_run.json
```

The runner prints step-by-step progress with the fallback level used
(`L1 exact` / `L2 semantic` / `L3 fingerprint-match` / `L4 human`) and
finishes with a summary table. Full audit artifacts are written to a new
directory under `sessions/<replay-session-id>/`.

---

## 6. Feature reference

### 6.1 Silent capture (teach mode)

The injected `grabber.js` listens globally for the following and captures
each with a rich fingerprint plus a settled post-condition snapshot:

| Kind | Fired on |
|---|---|
| `click` | Pointer click on an interactable element (button, link, input with no change semantics, role-based) |
| `input_change` | Debounced text input, plus select/date/checkbox/radio commit |
| `submit` | Form submission |
| `file_selected` | File input chosen |
| `navigate` | Initial load + SPA route changes (pushState/replaceState/popstate/hashchange) |
| `key` | Enter or Escape pressed inside a text field |

**What's filtered out** (to keep recordings clean):

- mousemove, scroll, hover
- focus/blur cycles that don't change state
- text keystrokes (captured as one settled value, not one event per key)
- redundant clicks on form inputs (input emits `change` directly)
- same-URL repeated navigations

### 6.2 Element fingerprint

For every captured event on an element, the grabber records:

- `test_id` (data-testid), `element_id`, `name`, `aria_label`, `role`
- `accessible_name` (computed via WAI-ARIA rules)
- `text`, `placeholder`, `tag`, `input_type`
- `css_path` (stops at first stable id/testid)
- `xpath`
- `ancestor_chain` (5 levels up, with tag/id/testid/role/class)
- `landmark` (nearest dialog / navigation / main / section with a name)
- `bbox` (for visual context and future L3 matchers)
- `frame_path` + `in_shadow_root` flags

Multiple locator alternatives per element means the replay side has
plenty of signal to recover from drift.

### 6.3 Auto-label heuristics

When annotating with `--auto`, labels and parameter bindings are
inferred from the fingerprint:

- `click` on `data-testid=btn-save-layout` → label `click_btn_save_layout`
- `input_change` on `#input-content-id` with value `"A-9001"` → label
  `fill_input_content_id`, parameter `input_content_id` with example
  `"A-9001"`
- `file_selected` → parameter type `file_path`
- Labels or element text containing keywords like `delete`, `publish`,
  `submit`, `send`, `finalize`, `register` → marked `requires_gate=true`

You can always override these interactively or by hand-editing the
skill JSON.

### 6.4 Parameter binding

Every field the operator fills becomes a candidate parameter. The
annotator proposes `mode="whole"` (replace the entire value) by default.
Two other modes are supported in the schema:

- `substring` — replace a substring inside the recorded value
- `template` — e.g. `{{content_id}}_hero.jpg` for filename patterns

For the POC, replay resolves `whole`-mode parameters. Substring and
template resolution are ready to wire up as the next increment.

### 6.5 Approval gates

A step marked `requires_gate=true` pauses replay before executing.
The gate callback (default: terminal `Confirm`) receives the step
context and returns approve/reject. Rejecting a step does not abort
the run — it logs a `gate` entry and continues (dependent tasks will
surface their own failures).

### 6.6 Four-level locator fallback (replay)

Per step, the runner tries in order:

| Level | Strategy |
|---|---|
| **L1 exact** | `data-testid` → `id` → `name=` → `aria-label=` |
| **L2 semantic** | `role + accessible name`; `placeholder`; containing text; css_path |
| **L3 fingerprint-match** | Queries all interactable elements in the page, scores each against the fingerprint (weighted role / accessible name / tag / text / placeholder / landmark), picks the top match if score ≥ 0.55 |
| **L4 human** | Pauses, screenshots, prints context, waits for operator to complete the step manually; then resumes |

L3 is currently a deterministic similarity matcher (no LLM), which
makes it reproducible and testable. The same seam is designed to be
swapped for an LLM-picked candidate from a distilled DOM when an
internal LLM is available. See §13 of `DOCS/requirement.md` for the
MCP/LLM plan.

### 6.7 Audit artifacts per session

```
sessions/<session-id>/
  ├── trace.jsonl              # every captured event, one JSON per line
  ├── meta.json                # session metadata (skill name, timestamps)
  ├── audit_log.jsonl          # only populated during replay sessions
  └── screenshots/
        └── <timestamp>_<label>.png   # before/after/gate/error shots
```

Replay sessions produce a separate session id with their own audit
log and screenshots. Teach and replay artifacts are never mixed.

### 6.8 Framework-agnostic capture

The grabber uses nothing framework-specific. It listens to standard
DOM events, walks the real DOM, and reads standard attributes. React,
Vue, Angular, Svelte, or static HTML portals are all handled the same
way.

Framework-specific "bonus" enrichment (e.g. React fiber tree via
`bippy`) is a planned plugin seam, not a requirement of the core.

### 6.9 Puppeteer Replay interoperability

`pilot.skill_models.Skill.to_puppeteer_replay()` down-converts any
captured skill to vanilla Puppeteer Replay JSON. That means any skill
can be run through plain `@puppeteer/replay` as a fallback, and we
benefit from an established format's tooling.

---

## 7. CLI reference

All commands run with `.venv/Scripts/python.exe -m pilot <command>` on
Windows, or `python -m pilot <command>` after sourcing the venv.

### `pilot doctor`

Quick feasibility check. Connects to the CDP endpoint, lists open pages.

```bash
pilot doctor [--cdp http://localhost:9222]
```

### `pilot teach`

Start a passive teach recording.

```bash
pilot teach <skill-name>
  [--base-url  http://localhost:5173]
  [--cdp       http://localhost:9222]
  [--sessions-dir sessions]
```

- Injects `grabber.js` into every frame.
- Streams events live to the terminal.
- Ends on Ctrl+C; prints a summary and the session id.

### `pilot annotate`

Convert a recorded trace into a reusable skill.

```bash
pilot annotate <session-id>
  [--name        <override-skill-name>]
  [--description "..."]
  [--base-url    http://localhost:5173]
  [--portal      sample_portal]
  [--auto]                          # non-interactive
  [--sessions-dir sessions]
  [--skills-dir   skills]
```

### `pilot run-skill`

Replay a learned skill.

```bash
pilot run-skill <path/to/skill.json>
  -p name=value [-p ...]            # repeatable
  [--params-file params.json]
  [--base-url http://localhost:5173]
  [--cdp      http://localhost:9222]
  [--sessions-dir sessions]
```

### `pilot run` (legacy deterministic runner)

Runs a hand-written `sample_tasks/*.json` task list through the
original manifest-based runner. Still works; useful for side-by-side
comparison.

```bash
pilot run sample_tasks/add_and_verify.json
```

---

## 8. Skill file format

Skills are JSON, stored in `skills/<name>.json`. The schema is defined
in `pilot/skill_models.py` (`Skill`, `SkillStep`, `ElementFingerprint`,
`ParamBinding`, `PostCondition`, `SkillParam`).

### 8.1 Minimal shape

```json
{
  "name": "curate_one_item",
  "description": "Create layout + schedule for a content item",
  "portal": "sample_portal",
  "base_url": "http://localhost:5173",
  "tags": [],
  "version": 1,
  "params": [
    {
      "name": "content_id",
      "type": "string",
      "description": "Content ID used across layout/schedule/thumbnails",
      "example": "A-9001",
      "required": true
    }
  ],
  "steps": [
    {
      "index": 0,
      "action": "navigate",
      "url": "http://localhost:5173/dashboard"
    },
    {
      "index": 1,
      "action": "click",
      "semantic_label": "click_nav_curation",
      "fingerprint": {
        "test_id": "nav-curation",
        "role": "link",
        "accessible_name": "Curation",
        "tag": "a",
        "css_path": "a[data-testid='nav-curation']",
        "xpath": "/html/body/div/div/aside/nav/ul/li[3]/a"
      }
    },
    {
      "index": 3,
      "action": "change",
      "value": "A-9001",
      "semantic_label": "fill_content_id",
      "param_binding": {
        "name": "content_id",
        "type": "string",
        "mode": "whole"
      },
      "fingerprint": { "test_id": "input-layout-content-id", "...": "..." }
    },
    {
      "index": 6,
      "action": "click",
      "requires_gate": true,
      "gate_reason": "saves the row — irreversible for this session",
      "semantic_label": "click_btn_save_layout",
      "fingerprint": { "test_id": "btn-save-layout", "...": "..." }
    }
  ]
}
```

Skills are human-readable and hand-editable. If a testid changes and
you want to repair without re-recording, just edit the fingerprint.

### 8.2 Supported action types

| Action | Per-step payload | Replay behavior |
|---|---|---|
| `navigate` | `url` | `page.goto(url)` |
| `click` | fingerprint | 4-level locator → `.click()` |
| `change` | fingerprint + `value` (param-resolved) | 4-level locator → `.fill()` or `.select_option()` |
| `submit` | (fingerprint ignored) | No-op — the adjacent click already submitted the form |
| `upload` | fingerprint + `file_path` (param-resolved) | 4-level locator → `.set_input_files()` |
| `key` | fingerprint + `value` | 4-level locator → `.press()` |
| `wait` | `wait_ms` | `page.wait_for_timeout()` |

---

## 9. Sample portal — what's in it

The bundled `sample_portal/` is a React + Vite app used as a realistic
proxy for real enterprise portals. Routes:

| Route | What it exercises |
|---|---|
| `/dashboard` | Trivial page — entry point |
| `/media-assets` | Table + search + modal create + delete + inline banner |
| `/curation` | Multi-tab page: Layout / Schedule / Thumbnails / Preview |
| `/schedule`, `/settings` | Placeholders |

Curation-specific complexity:

- Sub-tab switching with **dirty-state guard** modal
- **File upload** triggered via a visible label over a hidden `<input type=file>`
- **Delayed auto-dismiss banners** (3s)
- **iframe** preview with its own interactable elements (frame-context test)
- An **UnlabeledToolbar** section with no testids, no aria — forces the
  semantic/AX fallback to engage

Every intentional interactable element has `data-testid`. The unlabeled
section is kept deliberately bare so we can prove L2 fallback works on
real portal-style churn.

---

## 10. Automated tests

Two scripts are shipped to exercise the full system without human
driving. Both start vite, launch Chrome with CDP, and clean up.

### 10.1 `scripts/e2e_test.py`

```
TEACH → ANNOTATE (auto) → REPLAY (with new params) → VERIFICATION
```

Runs the full loop, drives the portal itself via Playwright (so the
grabber captures real DOM events), re-plays the resulting skill with
a different `content_id`, and verifies the rendered tables contain the
replayed value.

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/e2e_test.py
```

Expected final output:

```
[e2e] replay finished: 16 steps, 0 failures
[e2e] VERIFICATION OK: replayed row is present
[e2e] VERIFICATION OK: replayed schedule is present
```

### 10.2 `scripts/resilience_test.py`

Takes the saved skill, **corrupts L1 locators** (renames testids,
nullifies css_path and xpath) on three key click steps, then replays.
Proves that L2 semantic fallback covers the drift.

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/resilience_test.py
```

Expected final output:

```
[resil] results: 16 steps, 0 failures
[resil] levels used: {1: 13, 2: 3}
[resil] RESILIENCE OK: all corrupted steps resolved via fallback
```

---

## 11. Troubleshooting

### CDP connection refused

Symptom: `pilot doctor` prints `CDP connect failed`.

Likely causes:

1. Chrome was not launched with `--remote-debugging-port=9222`.
2. The flag was passed but a regular Chrome profile was already
   running — it ignores the flag to avoid conflicts. Use a dedicated
   `--user-data-dir` as shown in the setup.
3. Corporate Chrome policy blocks the flag. Check with your IT team;
   you may need the flag allowed on operator machines.

### No events captured during teach

Verify the grabber actually injected by opening Chrome DevTools →
Console in the portal tab and typing:

```js
!!window.__cp_grab_installed
```

Should print `true`. If not, the init script didn't run. Refresh the
page with `pilot teach` still running — on a fresh navigation the
init script will reinject.

### Text values captured as empty strings

This was a known bug (now fixed) where the debounce fired AFTER a
form reset. If you see it again, the grabber's flush-on-click-elsewhere
logic is not running. Check for page scripts that `stopPropagation()`
on click — they may prevent the capture-phase listener from seeing
the click. If so, add an early `mousedown` listener as a backup flush
point.

### Replay fails at L4 (human takeover)

Means none of L1/L2/L3 could locate the element. Options:

- Inspect the step's fingerprint in the skill JSON — is anything
  stable enough to find the element?
- Manually locate the element in the current portal; note its testid
  or accessible name.
- Edit the skill JSON to replace the failing fingerprint's test_id or
  accessible_name, then re-run.

In production, the human takeover callback would be your dashboard's
manual-takeover flow. The POC uses a terminal `input()` stub.

### React state resets on replay

React state held in `useState` is in-memory-only and is wiped on a
hard page reload. If a skill step is `navigate http://...`, that's a
full reload. Between replay and verification, always **navigate via
SPA (click sidebar/tab) not page.goto**, unless you deliberately want
to reset state.

For real portals backed by server state, this caveat doesn't apply.

### "Dirty-guard modal blocks further actions"

Happens when the recorded flow left a form dirty and the next
scripted click is on a different tab. Two options:

- During recording, always **save or cancel** before switching tabs.
- During replay, add a step that dismisses the dirty-guard modal
  (click `btn-dirty-discard`). Or teach the runner a general
  "unexpected modal handler" — currently out of POC scope.

---

## 12. How to extend

### Add a new adapter or skill for your own portal

1. Point Chrome at your portal (still using the dedicated CDP profile).
2. Run `pilot teach <name> --base-url https://your-portal/`.
3. Do the task once, Ctrl+C.
4. `pilot annotate <session-id>` — interactively review and name
   parameters meaningfully.
5. `pilot run-skill skills/<name>.json -p …` with new values.

No code changes required. If the portal has unstable attributes, you
may want to manually edit the skill JSON to prefer role+accessible
name over test_id.

### Upgrade the L3 matcher to an LLM

`pilot/skill_runner.py::_level3` is the single seam. Replace its
deterministic scorer with a call to an internal LLM that takes the
fingerprint + the distilled DOM (produced by `_JS_LIST_INTERACTABLES`)
and returns the chosen element's index plus a confidence. Respect the
confidence policy in `DOCS/requirement.md` §12 (high: execute, medium:
execute and assert hard, low: escalate).

### Ship as a browser extension

The same `pilot/overlay/grabber.js` becomes an MV3 content script.
Replace `window.__pilotCapture` with `chrome.runtime.sendMessage` to a
background script that forwards events to the Python service on
`localhost:5177` (or similar). This loses the zero-install friction
of CDP but gains cross-machine distribution and a toolbar UX.

---

## 13. File layout reference

```
curationpilot-poc/
├── DOCS/
│   ├── requirement.md          # Full product + architecture doc (source of truth)
│   └── GUIDE.md                # This guide
├── pilot/                      # Python package
│   ├── __init__.py             # Forces UTF-8 stdout on Windows
│   ├── __main__.py
│   ├── cli.py                  # Typer CLI: teach, annotate, run-skill, doctor, run
│   ├── browser.py              # CDP connect helper
│   ├── teach.py                # Teach-mode recorder
│   ├── annotate.py             # Annotation engine (auto + interactive)
│   ├── skill_runner.py         # Replay with 4-level fallback
│   ├── skill_models.py         # Pydantic: Skill, SkillStep, Fingerprint, etc.
│   ├── audit.py                # JSONL audit + screenshots
│   ├── models.py               # Legacy ToolResult, Task, TaskList
│   ├── runner.py               # Legacy manifest runner (kept for reference)
│   ├── adapters/               # Legacy hand-written adapters
│   │   ├── base.py
│   │   └── media_assets.py
│   └── overlay/
│       └── grabber.js          # The injected listener
├── sample_portal/              # React + Vite demo portal
│   └── src/
│       ├── App.jsx
│       ├── components/
│       │   └── Sidebar.jsx
│       └── pages/
│           ├── Dashboard.jsx
│           ├── MediaAssets.jsx
│           ├── Curation/
│           │   ├── index.jsx
│           │   ├── LayoutTab.jsx
│           │   ├── ScheduleTab.jsx
│           │   ├── ThumbnailsTab.jsx
│           │   ├── PreviewFrame.jsx
│           │   └── UnlabeledToolbar.jsx
│           ├── Schedule.jsx
│           └── Settings.jsx
├── sample_tasks/               # Legacy hand-written task lists
│   └── add_and_verify.json
├── scripts/
│   ├── launch_chrome_cdp.sh    # bash launcher
│   ├── serve_portal.sh
│   ├── e2e_test.py             # full teach-annotate-replay-verify loop
│   └── resilience_test.py      # L2 fallback under locator drift
├── sessions/                   # (gitignored) Per-run artifacts
│   └── <session-id>/
│       ├── trace.jsonl
│       ├── meta.json
│       ├── audit_log.jsonl
│       └── screenshots/
├── skills/                     # (optional to track) Saved skills
│   └── <name>.json
├── pyproject.toml
└── README.md
```

---

## 14. Quick command cheat sheet

```bash
# --- Setup (once) ---
py -m venv .venv
.venv/Scripts/python.exe -m pip install -e .
cd sample_portal && npm install && cd ..

# --- Session start (two terminals, left running) ---
cd sample_portal && npm run dev                        # Terminal A
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$USERPROFILE/.curationpilot-chrome-profile" \
  http://localhost:5173                                # Terminal B

# --- Sanity check ---
.venv/Scripts/python.exe -m pilot doctor

# --- Record a skill ---
.venv/Scripts/python.exe -m pilot teach my_skill
# (use the portal in Chrome; Ctrl+C when done)

# --- Turn the trace into a skill (auto mode) ---
.venv/Scripts/python.exe -m pilot annotate <session-id> --auto

# --- Replay with new parameters ---
.venv/Scripts/python.exe -m pilot run-skill skills/my_skill.json \
  -p content_id=A-3001 -p start_date=2026-06-01

# --- Run the bundled end-to-end and resilience tests ---
.venv/Scripts/python.exe scripts/e2e_test.py
.venv/Scripts/python.exe scripts/resilience_test.py
```

---

## 15. Limitations and next steps

Honest limitations of the current POC:

- **Three terminals to start a session.** A `pilot start` one-command
  wrapper would fold vite + Chrome + teach into one gesture.
- **No GUI.** All operator interaction is through the CLI. A minimal
  React dashboard at `localhost:3000` is the natural next step and is
  already specified in `DOCS/requirement.md` §6.9.
- **L3 is deterministic, not LLM-assisted.** Swap is ready when an
  internal LLM client is available. Confidence policy specced in §12.
- **Parameter substitution is "whole" mode only.** Substring and
  template modes defined in the schema but not yet resolved at replay.
- **Post-conditions are captured but not asserted.** Each step records
  what changed after it ran; the runner doesn't currently verify those
  changes. Adding it is straightforward and would catch silent replay
  failures.
- **Shadow DOM piercing is partial.** Fingerprints flag
  `in_shadow_root`, but the serializer doesn't yet walk the shadow
  tree. Add when a real portal needs it.
- **Browser extension shell not implemented.** Same grabber.js, MV3
  wrapper. Unblocks operator machines where the CDP flag is awkward.

---

*Document version: POC-1.0*  
*Status: teach/annotate/replay loop working end-to-end; drift resilience verified*
