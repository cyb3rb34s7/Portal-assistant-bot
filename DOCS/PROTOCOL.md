# JSON-RPC Event Protocol

> Wire contract between the Electron app (and CLI) and the Python agent.
> Every message is a single line of JSON written to stdout (agent → host)
> or stdin (host → agent). UTF-8. Newline-terminated. No partial writes.

> **Versioning.** Every message includes `"v": 1`. Future breaking
> changes bump the version; backwards-compatible additions don't.

> **Stability tier.**
> - **stable** — locked; do not change shape.
> - **provisional** — may change before v1 ships; loud notice in changelog.

---

## 1. Transport rules

- Newline-delimited JSON (NDJSON) over stdio.
- One message per line. No trailing whitespace.
- Lines that cannot be parsed as JSON are logged by the host as
  `agent.log` warnings and dropped.
- The agent flushes stdout after every message.
- The host flushes stdin after every command.
- Heartbeats: agent emits `{"type":"agent.heartbeat","v":1,"ts":...}`
  every 30 seconds when idle so the host can detect dead agents.

## 2. Message envelope

Every message is an object with at least:

```json
{
  "v": 1,
  "type": "<message-type>",
  "ts": "2026-04-28T15:04:05.123Z"
}
```

- `v` (int, required) — protocol version.
- `type` (string, required) — discriminator.
- `ts` (string, optional) — ISO-8601 UTC. Required on agent → host.

Additional fields are message-type-specific.

## 3. Direction conventions

- **Host → Agent** are commands. They cause state transitions.
- **Agent → Host** are events. They report state.
- The agent is single-task at a time in v1: one `task.submit` runs
  to completion (or `task.cancel`) before another is accepted.
- A response to `task.submit` is not a single reply; it's a stream of
  events ending in either `task.completed` or `task.failed`.

---

## 4. Host → Agent commands `[stable]`

### 4.1 `task.submit`

Start a new task.

```json
{
  "v": 1,
  "type": "task.submit",
  "task_id": "t-abc123",
  "goal": "Curate this release and use these thumbnails",
  "attachments": [
    {"path": "/Users/.../release.pptx", "kind": "pptx"},
    {"path": "/Users/.../thumbs",       "kind": "folder"}
  ],
  "portal_id": "samsung_curation",
  "options": {
    "auto_approve_plan": false,
    "screenshot_every_step": true
  }
}
```

- `task_id` — host-generated id; echoed back on every event for that task.
- `goal` — operator's natural-language description.
- `attachments[]` — files/folders the operator dropped in. `kind` is a
  hint; agent infers actually.
- `portal_id` — selects which portal context file to load.
- `options.auto_approve_plan` — if `true`, plan executes without human
  approval. **For tests only.** UI never sets this.

### 4.2 `clarify.answer`

Answer a clarify question.

```json
{
  "v": 1,
  "type": "clarify.answer",
  "task_id": "t-abc123",
  "question_id": "q1",
  "answer_value": "B",
  "answer_label": "Column B - Asset ID"
}
```

If the user picked "type a different answer" the host sends:

```json
{"answer_value": "__custom__", "answer_label": "user's free text"}
```

### 4.3 `plan.approve` / `plan.reject`

```json
{"v":1,"type":"plan.approve","task_id":"t-abc123","plan_id":"p1"}
{"v":1,"type":"plan.reject", "task_id":"t-abc123","plan_id":"p1",
 "reason":"wrong skill"}
```

On reject, the agent re-enters the clarify/plan loop with the operator's
reason as additional context.

### 4.4 `pause.resolve`

Resolve a `paused` event with one of the suggested actions.

```json
{
  "v": 1,
  "type": "pause.resolve",
  "task_id": "t-abc123",
  "pause_id": "x1",
  "action": "retry",
  "payload": null
}
```

- `action` — one of `"retry"`, `"skip"`, `"abort"`, `"use_alternate"`.
- `payload` — required when `action == "use_alternate"`; the body comes
  from the matching `step.failed.suggestions[].payload`.

### 4.5 `task.cancel`

Cancel the current task. Agent emits `task.cancelled` after cleanup.

```json
{"v":1,"type":"task.cancel","task_id":"t-abc123"}
```

---

## 5. Agent → Host events `[stable]`

All events carry `task_id` (when bound to a task) and `ts`.

### 5.1 `agent.ready`

Sent once on agent startup, before any task work.

```json
{
  "v": 1, "type": "agent.ready",
  "agent_version": "0.1.0",
  "capabilities": {
    "ai_clients": ["bedrock","groq","openai"],
    "default_client": "groq",
    "supports_attachments": ["pptx","csv","folder","image","pdf"]
  }
}
```

### 5.2 `agent.heartbeat`

Liveness ping. Every 30s when idle.

```json
{"v":1,"type":"agent.heartbeat","ts":"...","status":"idle"}
```

### 5.3 `agent.log`

Diagnostic log (not user-facing in v1; surfaced in dev tools).

```json
{"v":1,"type":"agent.log","level":"info","message":"...",
 "context":{"k":"v"}}
```

### 5.4 `intake.extracted`

Result of the intake stage.

```json
{
  "v": 1, "type": "intake.extracted",
  "task_id": "t-abc123",
  "entities": {
    "content_items": [
      {"id":"A-9001","title":"...","thumbnail":"thumbs/A-9001.png"},
      ...
    ],
    "dates": ["2026-05-01"],
    "files_resolved": [
      {"path":"...","matched_to":"thumb_for_A-9001"}
    ]
  },
  "intake_warnings": ["A-9007 has no matching thumbnail"]
}
```

### 5.5 `clarify.ask`

Ask the user one or more clarification questions.

```json
{
  "v": 1, "type": "clarify.ask",
  "task_id": "t-abc123",
  "id": "q1",
  "question": "The PPT has 3 columns matching 'Asset ID'. Which one?",
  "options": [
    {"value":"B","label":"Column B - Asset ID","detail":"12 values"},
    {"value":"D","label":"Column D - Internal Ref","detail":"12 values"},
    {"value":"F","label":"Column F - External Code","detail":"8 values"}
  ],
  "allow_custom_answer": true,
  "priority": "medium"
}
```

### 5.6 `plan.proposed`

Present the plan for approval.

```json
{
  "v": 1, "type": "plan.proposed",
  "task_id": "t-abc123",
  "id": "p1",
  "summary": "Curate 12 items from release.pptx",
  "skill_summary": [
    {"skill_id":"curate_one_item","invocations":12}
  ],
  "steps": [
    {
      "idx": 1,
      "skill_id": "curate_one_item",
      "params": {"content_id":"A-9001","layout_row":1,"layout_position":1,
                 "schedule_start":"2026-05-01"},
      "param_sources": {
        "content_id":"pptx slide 3 cell B2",
        "schedule_start":"user reply to q2"
      }
    }
  ],
  "destructive_actions": [
    {
      "step_idx": 1, "kind": "publish", "reversible": false,
      "label": "publish A-9001 ... A-9012"
    }
  ],
  "estimated_duration_seconds": 360,
  "preconditions": ["Operator is logged into the portal"]
}
```

### 5.7 `step.started` / `step.progress` / `step.succeeded` / `step.failed`

```json
{"v":1,"type":"step.started","task_id":"...",
 "idx":1,"skill_id":"curate_one_item",
 "params":{...}}

{"v":1,"type":"step.progress","task_id":"...",
 "idx":1,"action":"click","test_id":"btn-save-layout",
 "screenshot_path":"sessions/.../step_001.png"}

{"v":1,"type":"step.succeeded","task_id":"...",
 "idx":1,"duration_ms":3210}

{"v":1,"type":"step.failed","task_id":"...",
 "idx":1,
 "error_kind":"locator_exhausted",
 "error_message":"could not find btn-save-layout via 4 fallback levels",
 "screenshot_path":"sessions/.../step_001_fail.png",
 "suggestions":[
   {"action":"retry",        "label":"Retry"},
   {"action":"skip",         "label":"Skip this step"},
   {"action":"abort",        "label":"Abort run"}
 ]}
```

### 5.8 `paused`

Run is paused; awaiting `pause.resolve` from host.

```json
{
  "v": 1, "type": "paused",
  "task_id": "t-abc123",
  "pause_id": "x1",
  "reason": "step_failed",
  "context": {"step_idx": 7}
}
```

### 5.9 `report.ready`

Final report has been written.

```json
{
  "v": 1, "type": "report.ready",
  "task_id": "t-abc123",
  "session_id": "abc123",
  "report_path": "sessions/abc123/report.md",
  "summary": "12/12 items curated. 1 warning.",
  "warnings": ["A-9007 published without thumbnail"]
}
```

### 5.10 Terminal events

```json
{"v":1,"type":"task.completed","task_id":"t-abc123","session_id":"abc123"}
{"v":1,"type":"task.failed",   "task_id":"t-abc123",
 "error_kind":"clarify_budget_exhausted","error_message":"..."}
{"v":1,"type":"task.cancelled","task_id":"t-abc123"}
```

Exactly one terminal event per submitted task.

---

## 6. Error kinds `[provisional]`

- `intake_failed` — couldn't parse attachments / extract entities.
- `no_matching_skill` — planner cannot map goal to any skill in library.
- `clarify_budget_exhausted` — too many clarify rounds; abort.
- `plan_rejected` — operator rejected and re-attempts also rejected.
- `locator_exhausted` — replay failed all 4 fallback levels.
- `unexpected_modal` — page state inconsistent with skill assumptions.
- `session_expired` — portal kicked the operator out.
- `agent_internal_error` — bug; details in `error_message`.

---

## 7. Sequencing

Per task, events flow in roughly this order. Optional events in
`[brackets]`.

```
task.submit (host)
  -> intake.extracted (agent)
  -> [clarify.ask  -> clarify.answer]*       (zero or more rounds)
  -> plan.proposed (agent)
  -> plan.approve | plan.reject (host)       (reject => loop back)
  -> [step.started -> step.progress*
       -> step.succeeded | step.failed]+     (one per plan step)
       (on step.failed: paused -> pause.resolve)
  -> report.ready (agent)
  -> task.completed | task.failed | task.cancelled (agent)
```

---

## 8. Notes for implementers

- The agent does not buffer events while waiting for a host reply. Once
  it emits `clarify.ask` / `plan.proposed` / `paused`, it suspends until
  the corresponding answer arrives.
- The host MUST handle event types it doesn't know by logging-and-
  ignoring (forward-compat). The agent MUST reject unknown command
  types with `agent.log{level:"error"}` and continue.
- Paths in events are relative to the agent's working directory unless
  otherwise documented. The host resolves them for display.
- Screenshots are written by the agent before the matching `step.progress`
  / `step.failed` event is emitted; the host can `fs.read` immediately.
