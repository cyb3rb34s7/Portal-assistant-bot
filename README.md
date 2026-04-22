# CurationPilot POC

A minimal, architecturally honest slice of the CurationPilot system:
a small React portal, and a Python package that drives it via Playwright + CDP
through the operator's real Chrome, with audit logging and a human gate.

## What this proves

- CDP connection to the operator's Chrome works end-to-end
- MCP-ready adapter pattern holds up in real code
- `ToolResult` contract flows cleanly through runner and audit log
- Deterministic Level 1 locators drive a realistic page
- Human approval gate actually pauses execution before an irreversible action
- Every action is captured as JSONL + before/after screenshots

## Layout

```
curationpilot-poc/
  sample_portal/       # React + Vite portal with a Media Assets page
  pilot/               # Python package: adapters, runner, CLI
  sample_tasks/        # Example task-list JSON files
  scripts/             # Helpers to launch Chrome (CDP) and the portal
  sessions/            # Per-run audit artifacts (gitignored)
```

## One-time setup

```bash
# Python
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium    # only used to fetch browsers; runtime uses your Chrome

# Portal
cd sample_portal && npm install && cd ..
```

## Run the POC

Three terminals.

**Terminal 1 — run the sample portal**

```bash
./scripts/serve_portal.sh
# http://localhost:5173
```

**Terminal 2 — launch Chrome with CDP enabled**

```bash
./scripts/launch_chrome_cdp.sh
```

This opens a dedicated Chrome profile (isolated from your daily browser) and
navigates to the portal. Leave the window open.

**Terminal 3 — run the pilot**

```bash
source .venv/bin/activate
python -m pilot doctor                  # sanity-check CDP connection
python -m pilot run sample_tasks/add_and_verify.json
```

Watch Chrome as the pilot navigates to Media Assets, creates two assets,
verifies them, and then pauses at a human gate before deleting one.
Type `y` in the terminal to approve. After the run, inspect
`sessions/<id>/audit_log.jsonl` and `sessions/<id>/screenshots/`.

## What comes next (deferred, by design)

- LangGraph in place of the simple runner (state + checkpointing)
- Level 2 semantic fallback locators
- Level 3 internal-LLM fallback with DOM distillation + confidence scoring
- Level 4 manual takeover UI
- Multi-page / multi-tab workflows
- MCP server wrapper over the adapter methods
