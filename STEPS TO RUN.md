# CurationPilot — Steps to run

This is the **end-to-end "I just cloned the repo, what do I run?"**
guide for the local UI flow. Two paths:

  1. **Sample portal demo** -- everything against the bundled
     `sample_portal` (no enterprise login required). Use this first
     to confirm your machine is set up.
  2. **Real portal** -- swap the `--portal-id` for one declared under
     `portals/<id>/context.yaml`.

The deterministic CLI (`pilot run-skill ...`) still works for headless
replays, but the supported operator surface is now the **React UI**
talking to **FastAPI** (`pilot serve`) over `/api/*`. The CLI commands
in this doc cover only one-shot reproduction; everything routine flows
through the UI.

---

## 0. Prereqs (one-time)

```powershell
# Python
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium

# Sample portal (only if you want the bundled demo)
cd sample_portal
npm install
cd ..

# React UI
cd curationpilot-app
npm install
cd ..

# LLM key (for intake / planner / reporter / annotate-LLM)
copy .env.example .env
# edit .env -- set GROQ_API_KEY=...
```

The defaults assume **Groq** for LLM calls (cheap + fast for the
intake / planner loop). Other providers are supported -- see
`pilot/agent/ai_client/`.

---

## 1. Three terminals -- the local stack

You need three things running concurrently. Each lives in its own
PowerShell window.

### Terminal 1 -- Sample portal (only for the demo path)

```powershell
cd sample_portal
npm run dev
```

Wait for `Local: http://localhost:5188/`. Vite locks to port 5188
(`--strictPort`) so the URL is stable for the planner / runner.

### Terminal 2 -- FastAPI server

```powershell
.\.venv\Scripts\Activate.ps1
python -m pilot serve
```

Wait for `Uvicorn running on http://127.0.0.1:5177`. This serves
`/api/*` for the React UI and proxies the WebSocket event stream.
**Restart it any time you edit Python code.**

### Terminal 3 -- React UI dev server

```powershell
cd curationpilot-app
npm run dev
```

Wait for `Local: http://localhost:5174/`. Open that URL in any
browser. Vite proxies `/api/*` to `http://127.0.0.1:5177` so the UI
talks to the FastAPI server transparently.

---

## 2. Path A -- Demo: teach a skill against the sample portal

The whole loop in five clicks.

1. **Open the UI** at <http://localhost:5174>.
2. **Home** -> **Launch portal browser**. This spawns the dedicated
   Chrome with `--remote-debugging-port=9222 --user-data-dir=...`
   pointed at `http://localhost:5188`. Wait for the doctor probe to
   show one tab.
3. **Teach** -> name the skill, pick `CurationPilot Sample Portal`
   from the portal dropdown, advance through the 5-step wizard.
   Step 3 is the recording -- click around the portal, save a
   layout, apply it. The wizard shows a live event count.
4. Stop recording, run the auto annotation (and optionally the
   LLM annotation in step 4 to get a v2 sidecar with semantic
   parameter names).
5. **Skills** tab now shows your skill. **Replay** tab is where
   you point the agent at it.

---

## 3. Path B -- Replay: type a goal, watch it drive

1. **Replay** in the sidebar.
2. Goal: `"Curate the contents in batch.csv into a featured-row
   layout, comment Spring drop"` (or anything natural-language --
   the planner reads your skill library and picks the right one).
3. Pick the portal from the dropdown.
4. Drag a CSV onto the dropzone (the planner extracts asset IDs
   from it during intake).
5. **Run replay**.

What you'll see, in order:

  - Stage badge progresses: `INTAKE` -> `CLARIFYING` (only if the
    goal is ambiguous) -> `PLAN READY` -> `RUNNING` -> `COMPLETED`.
  - **ClarifyModal** -- the planner asks one question at a time
    when params are missing. Pick an option or type a custom
    answer. There's a "Cancel task" link inside the modal in case
    a clarify loop gets stuck.
  - **PlanApprovalModal** -- summary, step list, **destructive
    actions are surfaced explicitly**. Irreversible actions
    (publish, delete) require human approval even with
    auto-approve checked.
  - **TracePane** -- one row per plan step. State dot turns blue
    while running, green on success, red on failure. Sub-action
    progress (`click btn-save-layout`) shows under the row. Heals
    appear inline as amber "healed: <old> -> <new>" boxes.
  - **PausedModal** -- when a step fails the agent pauses for an
    operator decision. Generic failures show retry / skip / abort.
    For `ambiguous_target` (multiple rows match where the
    recording targeted one), a **row picker** lists the candidates
    with their testid + visible text so you can choose which one
    to act on, then `pause.resolve { action: "use_alternate" }`
    sends your pick back.
  - **ReportPanel** -- when the run completes, the markdown
    report from `sessions/<id>/report.md` renders inline.

---

## 4. Path C -- Reproducing a specific replay from the CLI

Useful for headless testing or to bypass the UI.

```powershell
.\.venv\Scripts\Activate.ps1
python -m pilot run-skill skills\my_curation.json `
  --param input_csv_file=tests\fixtures\batch.csv `
  --param layout=grid_2x2
```

Skip `--base-url` -- the runner reads `base_url` from the skill
JSON so portal coupling is per-skill, not per-CLI-invocation.

---

## 5. Doctor probes

Quick reality check that nothing is broken:

```powershell
# Is FastAPI up?
curl http://127.0.0.1:5177/api/skills

# Is the sample portal up?
curl http://127.0.0.1:5188/api/search?q=A-90

# Is Chrome's CDP reachable?
python -m pilot doctor
```

The first two should return JSON; the third should print one or
more page URLs.

---

## 6. Pointing at a real portal

1. Add `portals/<your_portal_id>/context.yaml` with:

   ```yaml
   portal_id: your_portal_id
   name: Your Portal
   base_url: https://your-portal.example.com
   page_map: []
   field_conventions: []
   ```

2. Click **Launch portal browser** in the UI -- when you don't
   pass an explicit URL it uses the most-recent target. From the
   CLI:

   ```powershell
   python -m pilot teach my_skill --portal-id your_portal_id
   ```

3. From the UI's Teach wizard, your portal will now appear in the
   dropdown alongside the sample portal.

There is no "default portal." The runner refuses to drive Chrome
without an intentional `portal_id` or `--base-url`. CurationPilot
drives any portal; baking a URL into the agent would couple it to
one.

---

## 7. Common issues

  - **WebSocket /api/events 404** -- `pip install websockets`
    inside `.venv` (already in `pyproject.toml` deps; only an
    issue if your `pip install -e .` predates that change).
  - **Vite serves the wrong app on 5188 / 5174** -- service
    workers from a previous project can hijack a port.
    DevTools -> Application -> Service Workers -> Unregister, or
    use a fresh Chrome profile.
  - **"Sync Playwright API inside the asyncio loop"** -- you're
    on an older `pilot serve`; teach setup must run on the
    dedicated single-thread executor. Restart the FastAPI server
    after a `git pull`.
  - **Replay clarifies in a loop and never plans** -- the LLM is
    underconverging. Provide more context in the goal (mention
    the file, the layout, the ID range) or attach the CSV; the
    planner reads attachments during intake.
  - **Step fails with `ambiguous_target`** -- the recording
    captured one specific row but replay finds many. The
    PausedModal will list candidates -- pick the right one. If
    the candidate list looks wrong, abort and check that the
    skill's recorded fingerprint actually templated the
    parameter (Skills tab -> open the skill JSON).
