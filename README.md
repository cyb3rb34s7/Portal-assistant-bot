# CurationPilot POC

A local, supervised browser automation tool that **learns a portal task
by watching an operator use the portal**, then **replays that task with
new inputs** — with a four-level locator fallback that survives UI
drift, and an audit trail for every action.

## What this proves

- Silent, framework-agnostic DOM capture of clicks / fills / navigations
  via a CDP-injected JS listener (works on React, Vue, Angular, plain HTML).
- Rich element fingerprints (testid, id, aria, role, accessible name,
  text, css path, xpath, landmark, bbox) for resilient replay.
- Auto-annotation of traces into parameterised, versioned skills.
- Four-level replay fallback (exact → semantic → fingerprint-match → human).
- Portable skill JSON, down-convertible to Puppeteer Replay format.
- Full audit artifacts per session: JSONL event log + before/after screenshots.

## Three-phase workflow

```
  TEACH              ANNOTATE            REPLAY
  -----              --------            ------
  pilot teach   ->   pilot annotate  ->  pilot run-skill
                     (auto or            (with new
                      interactive)        parameters)
```

## Quick start

**One-time setup:**

```bash
py -m venv .venv
.venv/Scripts/python.exe -m pip install -e .
cd sample_portal && npm install && cd ..
```

**Run a session (three terminals):**

```bash
# Terminal A — sample portal
cd sample_portal && npm run dev

# Terminal B — Chrome with CDP
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$USERPROFILE/.curationpilot-chrome-profile" \
  http://localhost:5173

# Terminal C — pilot
.venv/Scripts/python.exe -m pilot doctor                # sanity-check CDP
.venv/Scripts/python.exe -m pilot teach my_skill        # record; Ctrl+C to stop
.venv/Scripts/python.exe -m pilot annotate <id> --auto  # save skill JSON
.venv/Scripts/python.exe -m pilot run-skill skills/my_skill.json \
  -p content_id=A-3001                                  # replay with new params
```

## Automated tests (no human driving needed)

```bash
.venv/Scripts/python.exe scripts/e2e_test.py          # full loop, verifies state
.venv/Scripts/python.exe scripts/resilience_test.py   # drift test — L2 recovery
```

## Layout

```
curationpilot-poc/
├── DOCS/
│   ├── requirement.md    # Full product + architecture doc
│   └── GUIDE.md          # Operator + developer runbook (start here)
├── pilot/                # Python package (teach / annotate / skill_runner / audit)
│   └── overlay/grabber.js
├── sample_portal/        # React + Vite demo portal
├── sample_tasks/         # Legacy hand-written manifest (reference only)
├── scripts/              # Runner + E2E + resilience test scripts
├── sessions/             # (gitignored) Per-run audit artifacts
└── skills/               # Saved skills (JSON)
```

## Full documentation

See [DOCS/GUIDE.md](DOCS/GUIDE.md) for the complete feature reference,
CLI options, skill file format, troubleshooting, and extension guide.

See [DOCS/requirement.md](DOCS/requirement.md) for the overall product
vision, architecture decisions, and the broader roadmap beyond this
POC (LangGraph workflow engine, AI fallback, MCP promotion, dashboard
UI, multi-portal orchestration).

## Status

Teach → Annotate → Replay loop verified end-to-end on the sample portal.
Drift resilience verified — L2 semantic fallback recovers when `test_id`,
`id`, `css_path`, and `xpath` are all corrupted.

See [DOCS/GUIDE.md §15](DOCS/GUIDE.md) for limitations and next steps.
