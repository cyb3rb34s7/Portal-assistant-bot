## AI-Assisted Portal Workflow Automation
### Complete Architecture & Product Plan — Final

---

## 0. How to Use This Document

This document is the single source of truth for the CurationPilot project.
It incorporates all research, architectural discussions, framework evaluations,
and design decisions reached across multiple sessions.

A new engineering session should start here. No prior context is needed.

Sections:
1. Background and Problem
2. Constraints
3. Design Principles
4. What We Are Building (and What We Are Not)
5. High-Level Architecture
6. Component Breakdown
7. Technology Stack and Framework Decisions
8. Portal 2 Multi-Tab Strategy
9. Determinism Strategy
10. Human-in-the-Loop Model
11. Validation Strategy
12. AI Assistance and DOM Distillation
13. MCP Design Intent
14. Portal Knowledge Files
15. Full Tech Stack Summary
16. POC Milestone Plan
17. Key Risks and Mitigations
18. Success Metrics
19. User Scenario — Full Monday Curation Run
20. Design Decisions Reference
21. One-Sentence Architecture Summary

---

## 1. Background and Problem

A large OTT/ATV media platform (50M+ users) curates TV application homepages using a
set of internal web portals. Content operators receive a weekly editorial PPT from the
editorial team. They then manually perform a multi-step workflow across three internal
portals to update the homepage — registering content, assigning rows, uploading
thumbnails, setting schedules, and publishing.

### Current pain points

- 10–15 manual steps per curation run
- work spread across 3 portals
- Portal 2 contains multiple tabs with different responsibilities, creating
  significant switching overhead and state tracking burden
- high cognitive load from manually cross-referencing PPT → content IDs →
  thumbnail files → portal rows
- high risk of placement and scheduling errors
- a full curation run can consume nearly an entire working day

### The thumbnail problem (solved in POC scope)

Operators manually cross-reference thumbnail images from the PPT and upload them
to matching content. For the POC, this is solved by a pre-naming convention:
operators rename image files with the content ID or layout position before
the session starts (e.g. 12345_hero.jpg). The system reads the filename and
knows exactly where each image goes. No vision-based matching is needed.

---

## 2. Constraints

These are hard constraints that all architectural decisions must respect.

| Constraint | Detail |
|---|---|
| Portals cannot be modified | Must work on top of existing portal UIs |
| No external browser extensions | Org policy blocks Chrome extensions like Claude for Chrome |
| No external LLM APIs | All LLM calls must route to the internal ~70B model |
| No cloud execution | Everything runs locally on the operator's machine |
| No headless-only automation | Operator must be able to see and intervene |
| Data sovereignty | No data leaves internal infrastructure |
| Auth must be inherited | Cannot re-implement SSO or internal auth flows |

---

## 3. Design Principles

These principles drive all implementation decisions. When in doubt, refer back here.

### 3.1 Manifest-first execution
The system never browses portals and figures out what to do from scratch.
All execution is grounded in an approved manifest.

### 3.2 Deterministic execution first
Use deterministic automation wherever possible. AI is a fallback and assistant,
not the default click engine. This is the most important principle.

### 3.3 Human approval at trust boundaries
Any irreversible, risky, or uncertain action pauses for operator approval.

### 3.4 Real browser, real session
Operate on the operator's existing authenticated Chrome session via CDP.
Never launch a separate headless browser.

### 3.5 Explicit state ownership
The workflow engine owns state, dependencies, and progress.
The browser is a rendering surface. It holds no process state.

### 3.6 Auditability over magic
Every step is explainable, visible, and logged. No silent actions.

### 3.7 Graceful degradation
If automation fails at any level, the operator takes over manually and resumes.
The system never gets stuck in a way that requires a full restart.

### 3.8 MCP-ready interfaces
Portal adapter methods are designed as if they will eventually become MCP tools.
This costs nothing in the POC and makes future promotion trivial.

---

## 4. What We Are Building (and What We Are Not)

### What CurationPilot is

- a local supervised automation platform
- a workflow orchestration system with human checkpoints
- a deterministic browser automation layer with bounded AI fallback
- a manifest-driven, auditable, enterprise-safe tool

### What CurationPilot is not

- a generic chat-driven browser agent
- a Chrome extension
- a fully autonomous computer-use bot
- a pure RPA script bundle
- a cloud automation SaaS
- a free-roaming AI that decides what to do by browsing portals

### The core insight that drives the architecture

This is not primarily a "browser AI agent" problem.
It is a workflow orchestration, state tracking, and supervised execution problem
where browser automation is one component of the system.

The AI's role is narrow and bounded:
- parse and normalize the PPT
- assist recovery when deterministic locators fail
- explain failures to operators in plain language

Everything else is deterministic by design.

---

## 5. High-Level Architecture

```
OPERATOR'S MACHINE
│
├── Chrome Browser (existing, logged-in session)
│   ├── Portal 1 — registration / billing
│   ├── Portal 2 — curation (multiple tabs)
│   │     ├── Layout Tab
│   │     ├── Schedule Tab
│   │     ├── Thumbnails Tab
│   │     └── Preview Tab (read-only)
│   └── Portal 3 — publish / final configuration
│          ↑
│          │  Chrome DevTools Protocol (CDP)
│          ↓
│
├── CurationPilot Local Service  (Python / FastAPI — localhost)
│   │
│   ├── PPT Ingestion + Parser         (python-pptx)
│   ├── Manifest Normalizer            (Pydantic)
│   ├── Manifest Validator             (schema + business rules)
│   ├── Task Compiler                  (deterministic, manifest → typed tasks)
│   ├── Workflow Engine                (LangGraph)
│   │     ├── Portal 1 Subgraph
│   │     ├── Portal 2 Subgraph (tab-aware)
│   │     └── Portal 3 Subgraph
│   ├── Portal Adapters                (Playwright + CDP)
│   │     ├── Portal1Adapter
│   │     ├── Portal2Adapter
│   │     └── Portal3Adapter
│   ├── Execution Engine               (layered fallback strategy)
│   ├── DOM Distillation Utility       (accessibility tree → compact DOM)
│   ├── AI Assistance Layer            (internal LLM, bounded use only)
│   ├── Audit Logger                   (all actions, screenshots, approvals)
│   ├── Local Storage                  (SQLite + filesystem)
│   └── Internal LLM Client            (~70B model, internal infra)
│
└── Control Dashboard  (React — localhost:3000)
      ├── PPT upload
      ├── Manifest review and correction
      ├── Task checklist
      ├── Live action panel + screenshot
      ├── Human approval gate modals
      ├── Pause / Resume / Abort
      └── Manual takeover flow
```

---

## 6. Component Breakdown

### 6.1 PPT Ingestion and Parser

**Purpose**
Accept the weekly editorial PPT and extract all curation instructions into a
structured intermediate representation.

**Technology:** python-pptx

**Responsibilities**
- accept PPT file upload via dashboard
- parse slides and table structures
- extract: content IDs, titles, row assignments, layout types, position numbers,
  schedule dates, thumbnail references, and any notes
- identify ambiguous or missing fields
- optionally use internal LLM to normalize messy text (e.g. date formats,
  inconsistent content ID formatting)
- produce a raw extraction model — not yet the final manifest

**Key point**
The parser feeds a validation layer. It never drives execution directly.

---

### 6.2 Manifest Normalizer and Validator

**Purpose**
Convert raw extracted data into a clean, typed, operator-reviewable manifest.

**Responsibilities**
- normalize all fields to expected types
- validate required fields are present
- enforce schema via Pydantic
- detect duplicates, missing thumbnails, inconsistent row mappings
- check thumbnail filenames exist in the local thumbnails directory
- flag all issues for operator review
- produce a final approved manifest that is the immutable source of truth
  for the rest of the session

**Output schema**

```python
class ManifestItem(BaseModel):
    content_id: str
    title: str
    row: int
    position: int
    layout: str                     # e.g. "hero", "standard", "banner"
    thumbnail_file: str             # pre-named by operator, e.g. "12345_hero.jpg"
    thumbnail_path: str             # resolved absolute local path
    schedule_start: date
    schedule_end: date
    notes: str
    validation_status: str          # "valid" / "warning" / "error"
    validation_message: str | None

class ChangeManifest(BaseModel):
    session_id: str
    created_at: datetime
    ppt_filename: str
    items: list[ManifestItem]
    issues: list[str]
    approved: bool
    approved_at: datetime | None
    approved_by: str | None
```

---

### 6.3 Manifest Review UI

**Purpose**
Let the operator review, correct, and approve the extracted manifest before
any portal action begins. This is the most important safety gate in the system.

**Responsibilities**
- display all manifest items in an editable table
- highlight warnings and errors
- allow correction of any field
- show thumbnail file resolution status (found / not found)
- require explicit operator approval before proceeding
- lock manifest after approval — no further edits during execution

---

### 6.4 Task Compiler

**Purpose**
Transform the approved manifest into a typed, ordered list of execution tasks.

**Important design decision**
The task compiler is almost entirely deterministic — it applies business rules,
not LLM reasoning. The manifest is already clean and approved at this point.
The compiler just knows that "a content item with layout=hero in row 3" means
"run register_content on Portal 1, then assign_row_position on Portal 2 layout tab,
then set_schedule on Portal 2 schedule tab, then upload_thumbnail on Portal 2
thumbnails tab."

**When to use AI in the compiler**
Only when a manifest item contains an ambiguous instruction that cannot be resolved
by business rules alone. Even then, the LLM output is shown to the operator for
confirmation before the task is added to the queue.

**Output — typed task example**

```python
class CurationTask(BaseModel):
    task_id: str
    portal: str                     # "portal_1" / "portal_2" / "portal_3"
    tab: str | None                 # for portal 2 only
    action: str                     # maps to an adapter method name
    content_id: str
    params: dict                    # action-specific parameters
    depends_on: list[str]           # task_ids that must complete first
    requires_approval_gate: bool
    expected_result: str            # human-readable postcondition description
    status: str                     # pending / running / done / failed / skipped

# Example tasks compiled from a single manifest item
tasks = [
    CurationTask(
        task_id="t1",
        portal="portal_1",
        tab=None,
        action="register_content",
        content_id="12345",
        params={"title": "Show Name"},
        depends_on=[],
        requires_approval_gate=False,
        expected_result="content 12345 registered, registration confirmed"
    ),
    CurationTask(
        task_id="t2",
        portal="portal_2",
        tab="layout",
        action="assign_row_position",
        content_id="12345",
        params={"row": 3, "position": 2},
        depends_on=["t1"],
        requires_approval_gate=False,
        expected_result="content 12345 appears in row 3 position 2"
    ),
    CurationTask(
        task_id="t3",
        portal="portal_2",
        tab="schedule",
        action="set_schedule",
        content_id="12345",
        params={"start_date": "2026-05-01", "end_date": "2026-05-31"},
        depends_on=["t2"],
        requires_approval_gate=False,
        expected_result="schedule set and saved for content 12345"
    ),
    CurationTask(
        task_id="t4",
        portal="portal_2",
        tab="thumbnails",
        action="upload_thumbnail",
        content_id="12345",
        params={"file_path": "/thumbnails/12345_hero.jpg"},
        depends_on=["t2"],
        requires_approval_gate=False,
        expected_result="thumbnail uploaded and visible for content 12345"
    ),
]
```

---

### 6.5 Workflow Engine

**Technology:** LangGraph

**Purpose**
Run the entire workflow as a resumable, typed, checkpointed state machine.

**Why LangGraph over other frameworks**

We evaluated: LangGraph, OpenAI Agents SDK, AutoGen, CrewAI, Pydantic AI.

| Framework | Verdict |
|---|---|
| LangGraph | Best fit — first-class interrupt/resume for human gates, typed state, subgraphs, SQLite checkpointing, LLM-agnostic |
| OpenAI Agents SDK | OpenAI-centric, weak human gate support, good handoff model but wrong fit |
| AutoGen | Conversation-centric, overkill for sequential workflow, heavier setup |
| CrewAI | Parallel crew model, wrong mental model for sequential stateful workflow |
| Pydantic AI | Clean and typed but no native state machine or graph primitives |

LangGraph is also already in use by the team, reducing adoption friction.

**Anthropic API note**
The Anthropic API only serves Claude models — it cannot be used with the internal
~70B model. MCP (Model Context Protocol) is LLM-agnostic and is used as the
interface design standard for adapter tools. See Section 13.

**Responsibilities**
- session lifecycle management
- task queue progression
- checkpoint persistence via SQLite
- human gate pauses via LangGraph interrupt()
- retry policy enforcement
- error routing
- resume after interruption from exact last checkpoint

**State object**

```python
class CurationState(TypedDict):
    session_id: str
    manifest: dict
    manifest_approved: bool
    current_portal: str
    current_tab: str | None
    pending_tasks: list[dict]
    completed_tasks: list[dict]
    failed_tasks: list[dict]
    current_task: dict | None
    awaiting_human_gate: bool
    human_gate_reason: str | None
    last_action: str
    last_result: str
    last_screenshot_path: str | None
    recovery_mode: bool
    error_context: str | None
    retry_count: int
```

**Graph structure**

```
[START]
  → PPT Parse Node
  → Manifest Normalize + Validate Node
  → Human Gate: Approve Manifest          ← interrupt()
  → Task Compile Node
  → Portal 1 Subgraph
      → Task Loop Node (Portal 1)
      → Execution Node
      → Validation Node
      → Human Gate: Before Submission     ← interrupt()
  → Portal 2 Subgraph
      → Tab Router Node
      → Task Loop Node (Layout Tab)
      → Task Loop Node (Schedule Tab)
      → Task Loop Node (Thumbnails Tab)
      → Execution Node
      → Validation Node
      → Human Gate: Configurable batch    ← interrupt()
  → Portal 3 Subgraph
      → Task Loop Node (Portal 3)
      → Execution Node
      → Validation Node
      → Human Gate: Final Publish         ← interrupt()
  → Audit Archive Node
  → [END]
```

---

### 6.6 Portal Adapters

**Purpose**
Provide deterministic, portal-specific automation logic with interfaces designed
for future MCP promotion.

**Why adapters as code, not just knowledge docs**
Portal UI changes require code-level responses (updating locators), not just
markdown updates. Knowledge docs assist adapter development — they do not replace it.

**The MCP-ready interface contract**
Every adapter method follows four rules:

**Rule 1 — Primitive inputs only**
No internal state objects passed as arguments. Every input is an explicit parameter.

**Rule 2 — Standard ToolResult return type**

```python
class ToolResult(BaseModel):
    success: bool
    action_taken: str           # human-readable description
    output: dict | None         # structured output if any
    error: str | None           # error message if failed
    screenshot_path: str | None # post-action screenshot
    confidence: str | None      # set by AI fallback only: "high"/"medium"/"low"
```

**Rule 3 — Plain English docstring**
Every method has a docstring describing what it does, inputs, and success condition.
This becomes the MCP tool description with zero editing when promoted.

**Rule 4 — No undeclared side effects**
A method that assigns a row must not also save the page unless "save" is in the name.

**Portal 1 Adapter**

```python
class Portal1Adapter:
    def register_content(self, content_id: str, title: str) -> ToolResult:
        """
        Registers a content item in Portal 1.
        Navigates to the registration form, fills in content_id and title,
        and submits. Verifies the confirmation banner appears.
        Success condition: registration confirmation visible for content_id.
        """

    def verify_registration(self, content_id: str) -> ToolResult:
        """
        Verifies that content_id is registered in Portal 1.
        Searches the registered content list for content_id.
        Success condition: content_id appears in the registered items list.
        """

    def submit_registrations(self) -> ToolResult:
        """
        Submits all pending registrations in Portal 1.
        This action is irreversible and always requires human gate approval
        before the caller invokes this method.
        Success condition: submission confirmation page is displayed.
        """
```

**Portal 2 Adapter**

```python
class Portal2Adapter:
    def switch_to_tab(self, tab_context: str) -> ToolResult:
        """
        Switches Portal 2 to the specified tab context.
        tab_context must be one of: "layout", "schedule", "thumbnails", "preview".
        Verifies the correct tab is active before returning.
        Success condition: specified tab is active and page is loaded.
        """

    def assign_row_position(
        self, content_id: str, row: int, position: int
    ) -> ToolResult:
        """
        Assigns a content item to a specific row and position in the Layout tab.
        Opens the row editor for the given row, searches for content_id,
        assigns the position. Does not save — caller must invoke save_row separately.
        Success condition: content_id is selected in row at position in the editor.
        """

    def save_row(self, row: int) -> ToolResult:
        """
        Saves changes to the specified row in the Layout tab.
        Clicks the Save Row button for the given row number.
        Waits for page reload confirmation before returning.
        Success condition: page reloads and row shows updated content.
        """

    def set_schedule(
        self, content_id: str, start_date: str, end_date: str
    ) -> ToolResult:
        """
        Sets the schedule for a content item in the Schedule tab.
        Locates content_id in the schedule list, sets start_date and end_date.
        Saves the schedule entry.
        Success condition: schedule fields show correct dates after save.
        """

    def upload_thumbnail(self, content_id: str, file_path: str) -> ToolResult:
        """
        Uploads a thumbnail image for a content item in the Thumbnails tab.
        Locates the upload trigger for content_id and attaches file_path.
        Verifies the uploaded image thumbnail appears in the UI.
        Success condition: thumbnail preview visible for content_id.
        """

    def verify_layout(
        self, content_id: str, row: int, position: int
    ) -> ToolResult:
        """
        Verifies that content_id is correctly placed in row at position.
        Reads the current Layout tab state and checks the placement.
        Success condition: content_id found at row and position.
        """
```

**Portal 3 Adapter**

```python
class Portal3Adapter:
    def verify_configuration(self) -> ToolResult:
        """
        Performs a final verification of the complete homepage configuration.
        Checks that all expected content items are present with correct placements.
        Success condition: all manifest items are present in the preview.
        """

    def publish_homepage(self) -> ToolResult:
        """
        Publishes the homepage changes live.
        This action is irreversible and always requires human gate approval
        before the caller invokes this method.
        Success condition: publish confirmation is displayed.
        """
```

**Maintenance model**

| Change type | Response |
|---|---|
| Small UI label change | Update locator constant in adapter |
| Locator or layout change | Update adapter implementation |
| Workflow or flow change | Update adapter + task compiler |
| New portal | Write new adapter following same interface contract |
| New action type | Add method to existing adapter |

---

### 6.7 Execution Engine

**Purpose**
Execute one task at a time against the browser using a layered fallback strategy.

**Technology:** Playwright + CDP on operator's real Chrome

**The four-level execution strategy**

#### Level 1 — Deterministic locators
Primary execution path. Use stable Playwright locators — ARIA roles, labels,
explicit IDs, stable data attributes.

```python
# Examples
await page.get_by_role("button", name="Save Row").click()
await page.get_by_label("Content ID Search").fill(content_id)
await page.locator("[data-testid='row-editor-3']").click()
```

#### Level 2 — Semantic fallback
If primary locator fails, try text-based and role-based alternative selectors
defined as fallbacks in the adapter.

```python
# Examples
await page.locator("button:has-text('Save')").first.click()
await page.locator("[placeholder*='Search']").fill(content_id)
await page.get_by_role("button").filter(has_text="Save").click()
```

#### Level 3 — AI-assisted fallback (DOM distillation)
If semantic fallback also fails, call the internal LLM with a distilled DOM
snapshot of the current page and the task description.
See Section 12 for full design.

The LLM returns a ranked candidate element from the distilled DOM — not a
free-form instruction. The executor acts on it and runs post-action verification.
If verification fails, escalate to Level 4.

#### Level 4 — Human intervention
All automated strategies failed. Workflow pauses. Dashboard shows the operator
a screenshot, the failed task, and what was attempted. Operator completes the
step manually and clicks Resume. Workflow continues from next task.

**Why this layering is important**
The LLM is never the primary click engine. Levels 1 and 2 handle the vast majority
of actions on stable portals. Level 3 handles minor UI drift. Level 4 handles
genuine failures. The system never gets stuck.

---

### 6.8 Browser Connectivity Layer

**Technology:** Chrome DevTools Protocol (CDP)

**How it works**
The operator launches Chrome with the remote debugging flag enabled:

```
chrome --remote-debugging-port=9222
```

CurationPilot connects to the existing Chrome session via Playwright's CDP support.
The operator's existing authenticated session is fully inherited. No re-login.
No separate browser. The operator sees every action in their own browser window.

**Why CDP over headless**
- inherits all existing authentication including SSO and internal auth portals
- operator sees all actions in real time — builds trust
- operator can grab the mouse and take over at any moment
- enterprise Chrome policies that block headless detection do not apply
- no separate browser instance to manage

**Critical validation — do this in Milestone 0**
Some corporate Chrome deployments restrict the remote debugging flag via
group policy. This must be tested on an actual operator machine before
any build work begins. If blocked, work with IT to enable it on curation
operator machines specifically.

---

### 6.9 Control Dashboard

**Technology:** React frontend, FastAPI backend, localhost:3000

**Purpose**
Give the operator a transparent, trustworthy control surface for the entire run.

**Main views**

*Session header*
- PPT filename, session ID, start time
- overall progress: X of Y tasks complete
- current status: running / waiting / paused / blocked
- Pause / Resume / Abort always visible

*Manifest review panel*
- editable table of all manifest items
- validation status per item with inline warnings
- thumbnail file resolution status
- Approve Manifest button (locked after approval)

*Task checklist*
- all tasks across all portals and tabs
- color-coded status: pending (grey) / running (blue) / done (green) /
  failed (red) / awaiting approval (amber) / skipped (muted)
- expand any task to see its parameters and result

*Live action panel*
- last action taken (plain English)
- current browser screenshot (auto-refreshed)
- next planned action

*Human gate modal*
- appears at every approval checkpoint
- shows: what is about to happen, why it requires approval, screenshot
- Approve / Reject / Edit and Retry buttons
- Reject sends task to failed queue with operator comment

*Manual takeover panel*
- appears when Level 4 escalation occurs
- shows: failed task description, what was attempted, screenshot
- Resume button — operator clicks after completing step manually
- Skip Task button — marks task as manually skipped with comment

**Trust principle**
The dashboard always shows what just happened, what is currently running,
and what will happen next. The operator is never surprised.

---

### 6.10 Audit and Persistence Layer

**Technology:** SQLite (session state), filesystem (screenshots, logs)

**Purpose**
Make every run resumable, reviewable, and supportable.

**Data captured**

```
sessions/
  {session_id}/
    manifest_approved.json      # locked approved manifest snapshot
    tasks.json                  # full task list with statuses
    audit_log.jsonl             # every action, timestamp, result
    screenshots/
      {task_id}_before.png
      {task_id}_after.png
      gate_{gate_id}.png
    errors/
      {task_id}_error.txt
```

**Why this matters**
- sessions can be resumed from exact last checkpoint after interruption
- operators can review what happened and when
- support and debugging are possible without re-running
- compliance and change tracking if needed later

---

## 7. Technology Stack and Framework Decisions

### Framework evaluation summary

We evaluated all major agent and browser automation frameworks.

**LangGraph** — selected for orchestration
Best fit because the core challenge is workflow orchestration with human interruptions,
not freeform agent chat. First-class interrupt/resume, typed state, subgraphs,
checkpointing, LLM-agnostic.

**Playwright** — selected for browser automation
Strongest Playwright API for deterministic adapter execution. Multi-tab support,
accessibility tree access, CDP connection, file upload handling.

**Not selected: Browser-Use**
Excellent library but too coupled to an LLM-centric execution loop for this use case.
We borrow its DOM distillation technique without adopting it as a runtime dependency.

**Not selected: Chrome extension approach**
Blocked by org policy. Also weaker orchestration model for multi-portal stateful flows.

**Not selected: Pure computer-use agent (vision-based)**
Too non-deterministic for operational production workflows. Screenshot-per-action
is slow, expensive, and unreliable for repetitive structured tasks.

**Not selected: Pure RPA (Selenium/UiPath)**
Insufficient flexibility for ambiguous PPT inputs and recovery handling.
Breaks on any UI change with no graceful degradation.

**Internal LLM (~70B class)** — selected for all AI tasks
Required by org constraint. Suitable for: PPT normalization, DOM-bounded fallback
element selection, operator-facing failure explanations. Must be validated for
structured tool-calling output reliability in Milestone 0.

---

## 8. Portal 2 Multi-Tab Strategy

Portal 2 is the operationally complex portal. Work is distributed across 4 tabs.
Each tab has different responsibilities and different save behaviors.

### The core problem

If the workflow treats Portal 2 tab-by-tab (all layout tasks, then all schedule tasks,
then all thumbnails), operators must mentally track which content items were processed
in each tab. But if it goes content-item-by-item (all tasks for item 1, then item 2),
the system switches tabs too frequently and risks unsaved state.

### The solution — compiler-driven tab grouping

The task compiler groups tasks by tab while maintaining content dependency ordering.

All layout tasks for all content items → save all rows → all schedule tasks →
all thumbnail uploads. Within each tab group, tasks are ordered by content item.

This minimizes tab switching while preserving required dependencies.

### Tab state protection

The Portal 2 adapter tracks unsaved state. The `switch_to_tab` method checks for
unsaved changes before switching. If unsaved changes exist, it calls `save_row`
first. This prevents silent data loss from premature tab switching.

### State ownership

The LangGraph state object tracks which tab is currently active, which tasks
are complete per tab, and which content items have been fully processed across
all tabs. The browser holds none of this state — it is purely a rendering surface.

---

## 9. Determinism Strategy

This is the most important engineering concern in the system.

| Risk | Mitigation |
|---|---|
| LLM invents tasks | Tasks come from compiled manifest only — no open-ended browsing |
| Wrong content selected | Task carries explicit content_id, adapter verifies selection |
| Wrong row or position | Task contains target values, validator checks postcondition |
| Wrong image uploaded | Filename from approved manifest, validated against local directory |
| Locator breaks | Four-level fallback: deterministic → semantic → AI → human |
| Portal flow changes | Update adapter code and knowledge doc |
| Irreversible action without approval | Hardcoded human gate before all submit/publish/billing |
| Session interrupted | LangGraph SQLite checkpointer — resume from exact last task |
| AI fallback goes wrong | Confidence scoring — low confidence skips execution entirely |
| LLM hallucinates element | DOM distillation — LLM picks from real elements only |
| Repeated failure loops | Bounded retries (max 3) then automatic escalation to human |
| Hidden agent behavior | Dashboard and audit log show every action |

### The determinism principle

Determinism comes from typed inputs, compiled tasks, portal adapters,
explicit validation, and approval checkpoints.
Not from hoping the LLM behaves consistently.

---

## 10. Human-in-the-Loop Model

### Mandatory gates (always pause, never bypass)
- manifest approval before execution starts
- Portal 1 billing or registration submission
- Portal 3 final publish
- any Level 3 AI fallback result with confidence = low
- any task that exceeds retry limit

### Configurable gates (operator can adjust frequency)
- after every N content items in Portal 2
- after each portal subgraph completes
- before thumbnail upload batch

### Manual takeover flow
1. system pauses and shows screenshot
2. operator sees failed task and what was attempted
3. operator performs the step manually in their browser
4. operator clicks Resume in dashboard
5. workflow continues from next task
6. manual intervention is logged in audit trail

### What happens on Reject at a gate
- task is marked as rejected with operator comment
- workflow does not proceed to dependent tasks
- operator can edit the manifest item and requeue, or skip

---

## 11. Validation Strategy

Validation exists at multiple levels across the pipeline.

### Input validation (before execution)
- manifest schema validation via Pydantic
- duplicate content ID detection
- required field completeness checks
- thumbnail file existence checks against local directory
- date range validity checks

### Execution validation (during execution)
- page or tab context verification before each action
- post-action state verification against expected_result
- success indicator checks (confirmation banners, value changes)
- screenshot capture at every gate and every failure

### Session validation (after completion)
- no task silently skipped
- all required checkpoints completed and approved
- final state checked against full manifest

---

## 12. AI Assistance and DOM Distillation

### Where AI is used

| Use case | AI involvement | Bounded by |
|---|---|---|
| PPT text normalization | Yes — primary | Schema validation after |
| Manifest ambiguity resolution | Yes — with operator confirmation | Human approval |
| Level 3 execution fallback | Yes — element selection only | Distilled DOM |
| Failure explanation to operator | Yes — language generation | Factual context only |
| Task compilation | No — deterministic | Business rules |
| Portal navigation | No | Adapter code |
| Approval decisions | No | Always human |

### DOM Distillation — design

Before calling the LLM in fallback mode, the executor strips the current page
DOM to only interactable elements. This is the same approach used by Browser-Use
internally — we implement the same technique without the library dependency.

**What gets kept**
- all buttons with visible text and aria-label
- all input fields with label, placeholder, and type
- all select dropdowns with label and current value
- all links with meaningful text
- all elements with explicit role attributes (dialog, tab, menu, etc.)
- all elements with data-testid or aria-label attributes

**What gets stripped**
- decorative divs and layout containers
- script and style tags
- hidden or display:none elements
- duplicate wrapper elements
- elements with no interactive purpose

**Implementation**
The `read_dom()` utility queries Playwright's accessibility tree via CDP and
formats the result into a compact DistilledDOM model. Approximately 100–150 lines.
A full page DOM of 50,000+ tokens becomes a 200–500 token structured list.

```python
class DistilledDOM(BaseModel):
    page_title: str
    current_url: str
    elements: list[DOMElement]

class DOMElement(BaseModel):
    index: int
    type: str               # button / input / select / link / tab / dialog
    label: str
    current_value: str | None
    is_active: bool
    locator_hint: str       # Playwright locator for direct execution
```

**The fallback prompt**

```
TASK: {task.action_description}
CONTENT ID: {task.content_id}
PORTAL CONTEXT: {relevant excerpt from portal knowledge doc}

CURRENT PAGE — INTERACTIVE ELEMENTS:
{distilled_dom.formatted_list}

The deterministic locator for this task failed.
From the elements above, which should be interacted with to complete this task?

Respond in JSON only:
{
  "element_index": <int>,
  "element_label": "<string>",
  "action": "click" | "fill" | "select",
  "fill_value": "<string or null>",
  "confidence": "high" | "medium" | "low",
  "reason": "<one sentence>"
}
```

**Confidence policy**

| Confidence | Action |
|---|---|
| high | Execute action, verify postcondition |
| medium | Execute action, verify aggressively, always screenshot |
| low | Skip execution entirely, escalate to human gate |

This means the LLM's own uncertainty drives the escalation policy.
The system degrades gracefully rather than executing a low-confidence guess.

**Why this works**
The LLM cannot hallucinate a page element that does not exist — it picks from
a real, current, distilled list. The output is always actionable. Confidence
scoring gives the executor a clear policy rather than blind trust.

---

## 13. MCP Design Intent

### What MCP is

MCP (Model Context Protocol) is an open, LLM-agnostic protocol for standardizing
how agents call tools. Think of it as REST for agent tool interfaces.

It was introduced by Anthropic but is not tied to the Anthropic API or Claude.
Any LLM that supports tool calling can act as an MCP client, including the
internal ~70B model via an adapter layer.

### Why we do not implement MCP in the POC

The POC uses direct Python adapter calls. This is simpler and sufficient for
a single-operator local tool.

### Why we design adapters as if they will become MCP tools

When a second use case emerges — a chat interface for ad-hoc portal actions,
a scheduling assistant, a second agent that also needs browser control —
the adapter interfaces are already promotable to MCP tools with zero rewrite.

Without this design intent, a second use case requires either duplicating the
browser interaction code or tightly coupling the new tool to CurationPilot's
internals. Both are bad outcomes.

### What "MCP-ready" means in practice

All four adapter interface rules in Section 6.6 already produce MCP-ready methods.
When the time comes, the promotion path is:

1. Write a thin MCP server wrapper (Python, ~50 lines of boilerplate)
2. Register each adapter method as an MCP tool using its existing docstring
3. The implementation does not change

No rewrite. No interface redesign. The pattern is already in place.

---

## 14. Portal Knowledge Files

### Purpose

Human-maintainable markdown files that document portal structure, labels, quirks,
and expected workflows. Located in `/knowledge/` directory.

### Three specific roles

**Role 1 — Adapter development reference**
When writing or updating a portal adapter, the developer reads the knowledge file
to understand what they are automating before writing locators.

**Role 2 — Fallback prompt enrichment**
When Level 3 AI fallback is invoked, the relevant portal knowledge excerpt is
included in the fallback prompt as additional context about portal-specific quirks.

**Role 3 — Operator and developer reference**
Documents what operators should expect to see and do during manual interventions.

### What knowledge files do not replace
- portal adapter code
- deterministic locators
- execution validation logic

### Example — portal2.md

```markdown
# Portal 2 — Content Curation Portal

## Tab Structure
- Layout Tab (/curation/layout): Assign content to rows and positions
- Schedule Tab (/curation/schedule): Set start and end dates per content item
- Thumbnails Tab (/curation/thumbnails): Upload image assets
- Preview Tab (/curation/preview): Read-only — do not interact during automation

## Layout Tab — Known Quirks
- The "Save Row" button only appears after a field is modified — not on load
- After saving, the page refreshes automatically — wait for reload before next action
- Navigating away without saving discards all changes silently
- The row editor opens as a modal overlay — main page is not interactive while open
- Content search in the row editor requires exact content ID match

## Schedule Tab — Known Quirks
- Schedule fields are disabled until the content item has a row assignment
- Start date must precede end date or the save button stays greyed out
- Saving schedule does not trigger a page reload — no need to wait

## Thumbnails Tab — Known Quirks
- Upload input is a hidden file input triggered by clicking a visible "Upload" label
- Accepted formats: JPG and PNG only — other formats produce a silent failure
- After upload, the thumbnail preview takes 2–3 seconds to appear

## Common Failure Patterns
- "Save Row" button not found → modal did not open correctly, retry open
- Content ID not found in search → check if item is registered in Portal 1 first
- Tab switch loses unsaved changes → always save before switching tabs
- Thumbnail upload appears to succeed but preview missing → check file format
```

---

## 15. Full Tech Stack Summary

| Layer | Technology | Rationale |
|---|---|---|
| Local API service | FastAPI | Core service runtime, already in team stack |
| Workflow engine | LangGraph | State machine, human gates, checkpointing |
| Browser automation | Playwright | Deterministic adapter execution, accessibility tree |
| Browser connection | CDP on real Chrome | Inherits auth, operator visibility |
| PPT parsing | python-pptx | Structured extraction |
| Schema validation | Pydantic | Manifest and task type safety |
| DOM distillation | Custom Playwright utility | Accessibility tree → compact DOM, ~100-150 lines |
| AI fallback + parsing | Internal ~70B model | Org constraint, bounded use only |
| Dashboard | React | Operator control surface |
| Session state | SQLite via LangGraph checkpointer | Resumable sessions, local |
| Audit storage | Filesystem | Screenshots, logs, manifests |
| Future MCP layer | MCP server (deferred) | Adapter interfaces already MCP-ready |

### Optional additions post-POC
- Redis for coordination if multi-operator grows
- Postgres if SQLite becomes insufficient
- Policy engine for role-based approval routing
- OCR only if PPT quality requires it

---

## 16. POC Milestone Plan

### Milestone 0 — Feasibility validation (Week 1)
**Goal:** Prove the critical unknowns before any major build work.

Deliverables:
- confirm Chrome CDP access works on a real operator machine
- automate one golden-path action on one real portal via Playwright
- confirm file upload works via CDP-connected browser
- test internal LLM on a bounded tool-calling prompt with structured JSON output
- validate internal LLM reliability for DOM element selection task

**This milestone is a go/no-go gate. Do not proceed to M1 until M0 passes.**

---

### Milestone 1 — Manifest pipeline (Week 2)
Deliverables:
- PPT upload endpoint
- PPT parser
- manifest normalizer and validator
- manifest review and approval UI

---

### Milestone 2 — Browser layer foundation (Week 3)
Deliverables:
- Playwright runtime with CDP connection
- base adapter interface and ToolResult model
- Portal 1 adapter (basic registration flow)
- screenshot capture and logging
- `read_dom()` DOM distillation utility (first version)

---

### Milestone 3 — Single-portal workflow (Week 4)
Deliverables:
- LangGraph workflow for Portal 1 only
- task compiler (Portal 1 rules)
- task execution loop
- human gate (before Portal 1 submission)
- resume after interruption
- basic dashboard task checklist

---

### Milestone 4 — Fallback and recovery (Week 5)
Deliverables:
- Level 2 semantic fallback locators in Portal 1 adapter
- Level 3 AI fallback with DOM distillation and confidence scoring
- Level 4 manual takeover flow
- error and recovery UI panel

---

### Milestone 5 — Portal 2 multi-tab flow (Weeks 6–7)
Deliverables:
- Portal 2 adapter (layout, schedule, thumbnails, tab switching)
- tab-aware task grouping in task compiler
- unsaved-change protection in tab switch
- Portal 2 subgraph in LangGraph
- configurable batch human gates

---

### Milestone 6 — Full 3-portal orchestration (Weeks 8–9)
Deliverables:
- Portal 3 adapter
- full 3-portal workflow graph
- all human gates wired
- full audit export
- complete dashboard

---

### POC total estimate
**9–10 weeks** depending on portal complexity and internal LLM reliability.

This is a realistic estimate that accounts for portal quirks and adapter tuning.
Milestones 5 and 6 carry the most schedule risk.

---

## 17. Key Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Corporate Chrome blocks CDP | Medium | Critical | Validate in M0, escalate to IT if needed |
| Internal LLM weak at tool-calling | Medium | High | Test in M0, keep deterministic path as primary |
| Portal markup unstable | Medium | High | Multi-level locator fallback in adapters |
| PPT format drifts significantly | Medium | Medium | Modular parser, validation-heavy |
| Operators distrust automation | Medium | Medium | Strong dashboard transparency, manual takeover |
| Timeline slips on portal quirks | High | Medium | Prove golden paths early, stage scope aggressively |
| File handling errors | Low | Medium | Validate manifest against local files before execution |
| Tab switching loses state | Medium | Medium | Unsaved state tracking in Portal 2 adapter |
| Retry loops cause confusion | Medium | Medium | Bounded retries (3 max), explicit escalation |
| MCP promotion never happens | Low | Low | Design is complete, no cost if unused |

---

## 18. Success Metrics

### Operational metrics
- total curation run duration (target: under 2 hours)
- operator active time per run (target: under 30 minutes)
- percentage of tasks completed without human intervention
- number of manual interventions per run
- checkpoint approval time

### Quality metrics
- incorrect row or position placement rate
- missed content item rate
- wrong thumbnail upload rate
- failed publish prevention events

### Adoption metrics
- operator satisfaction score
- sessions completed versus abandoned
- time saved per week across team
- number of resumed sessions completed successfully

---

## 19. User Scenario — Full Monday Curation Run

**Persona:** Priya, content curation operator. Every Monday she receives a PPT
from the editorial team listing the week's homepage changes for 12 content items.

---

**8:30 AM — Upload and Review (15 minutes)**

Priya opens CurationPilot at localhost:3000. She uploads this week's PPT.
The system extracts 12 content items in about 20 seconds and displays the
manifest in a review table.

One item has a warning: the PPT listed "Row 1" for content ID 54321 but the
same item appears in Row 1 elsewhere in the manifest. The system flagged the
conflict. Priya corrects the row to 4. She also sees that the thumbnail for
content ID 67890 was not found in the local thumbnails folder. She places the
file and the status updates to resolved.

She clicks Approve Manifest.

---

**8:45 AM — Portal 1 Registration (automated, ~12 minutes)**

CurationPilot opens Portal 1 in Priya's existing Chrome session. She watches
the task checklist advance as the system registers each of the 12 content items.
All complete without issue.

A human gate modal appears:

> "Ready to submit 12 registrations on Portal 1. This action is irreversible.
> Screenshot of confirmation page is attached. Approve?"

Priya verifies the screenshot and clicks Approve.

---

**9:00 AM — Portal 2 Layout Tab (automated, ~20 minutes)**

The system switches to Portal 2's Layout tab. It works through all 12 content
items — opening each row editor, searching by content ID, assigning row and position,
saving each row.

On item 8, the Level 1 locator for the Save Row button fails (the portal rendered
a slightly different button label this week). Level 2 semantic fallback finds a
button with "Save" in the text. It succeeds. The task completes without escalation.
Priya sees this noted in the task detail: "Semantic fallback used — Save Row."

---

**9:20 AM — Portal 2 Schedule and Thumbnails (automated)**

The system switches to the Schedule tab and sets all 12 schedules.
Then switches to Thumbnails and uploads all 12 pre-named image files.

A batch approval gate appears after every 4 items. Priya approves with a quick
glance. One thumbnail upload triggers Level 3 AI fallback — the upload label
text had changed. The LLM selects a candidate element with confidence=medium.
The system executes it, verifies the thumbnail preview appears, and continues.

---

**9:40 AM — Portal 3 Publish (automated + final gate)**

The system runs through Portal 3 verification and configuration tasks.
Then stops:

> "All tasks complete. Ready to publish homepage changes. These will go live
> immediately. Full layout screenshot attached. 12 of 12 items confirmed.
> Approve?"

Priya does a final review of the layout screenshot. Everything matches the PPT.
She clicks Approve. The homepage is published.

---

**9:48 AM — Complete**

Total elapsed time: 78 minutes.
Priya's active time: manifest review (15 min) + 5 gate approvals (8 min) +
1 manual intervention that was handled by fallback automatically.
Effective active time: ~23 minutes.

The session audit log is archived locally with all screenshots, gate approvals,
fallback events, and timestamps.

---

## 20. Design Decisions Reference

| Decision | Choice | Rationale |
|---|---|---|
| Primary execution strategy | Deterministic Playwright adapters | Reliability, debuggability, no LLM cost on happy path |
| AI role | Bounded fallback + parsing only | Prevents non-determinism from being the default |
| DOM input to AI | Distilled accessibility tree | Prevents hallucination, fits in small token budget |
| Confidence scoring | Required on all AI fallback outputs | Policy-based escalation without manual rules |
| Adapter interface design | MCP-ready signatures | Future protocol promotion with zero rewrite |
| State ownership | LangGraph state object | Browser holds no process state |
| Browser connection | CDP on real Chrome | Inherits auth, operator visibility, no headless issues |
| Human gate policy | Hardcoded at trust boundaries + configurable at batches | Safety at irreversible actions, flexibility elsewhere |
| Maintenance model | Adapter code + knowledge docs | Code for locators, docs for context and quirks |
| LLM vendor | Internal ~70B model only | Org constraint, data sovereignty |
| Orchestration framework | LangGraph | Human gates, subgraphs, typed state, team familiarity |
| Browser automation library | Playwright (direct) | Best CDP support, accessibility tree, file upload |
| DOM distillation | Custom utility based on Browser-Use approach | Technique borrowed, not the library |
| MCP in POC | Not implemented | Design intent preserved, complexity deferred |
| Task generation | Deterministic compiler | Business rules, not LLM planning |
| Session persistence | SQLite via LangGraph checkpointer | Local, zero-config, resumable |

---

## 21. One-Sentence Architecture Summary

CurationPilot is a local, manifest-driven, human-supervised workflow automation
system that uses LangGraph for orchestration, Playwright portal adapters
(designed for future MCP promotion) as the primary deterministic execution layer,
a DOM-distillation-based AI fallback with confidence-scored element selection
for resilience against UI drift, and an internal LLM narrowly scoped to PPT
parsing, bounded fallback recovery, and operator-facing explanations — with
all data, execution, and LLM calls remaining entirely within the organization's
infrastructure.

---

*Document version: Final-POC-1.0*
*Status: Ready for engineering kickoff*
*Next step: Milestone 0 — Feasibility validation***