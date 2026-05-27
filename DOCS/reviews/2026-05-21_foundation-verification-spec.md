# CurationPilot — Foundation-Phase Implementation Verification

## Context

You produced the 50-work-item plan saved at:
`D:\PROJECTS\Portal Assistant\Portal-assistant-bot\DOCS\reviews\2026-05-21_fix-everything-plan.md`

We've now implemented the foundation phase (WI-01 through WI-07
minus WI-05 and WI-06 which are deferred to the next batch). Commits
landed on branch `feat/enterprise-portal-and-additive-sprint`:

- `77937c8` WI-01 schema foundation: ActionType extended,
  StepEffect family (NavigationEffect, PopupEffect, DownloadEffect,
  ModalEffect, ToastEffect, NewTabEffect, StateChangeEffect, network
  + dom expectation lists), ReplayPolicy, ParamProvenance,
  StepProvenance, Skill.schema_version, runner stubs for
  unimplemented action types.

- `9876d7c` WI-02 causality: TraceEvent extended with event_id,
  interaction_id, caused_by, sequence, source, monotonic_ts,
  raw_event_kind, page_state_before/after. Grabber uses
  _rootAttribution for user actions and _attribution for
  consequences. Annotator builds a causality graph and assigns
  synthetic ids to legacy traces.

- `620438a` WI-03 control metadata: ElementFingerprint extended
  with control_kind (28 control types), value_kind, options_snapshot,
  selected_options, ARIA state flags, disabled, readonly,
  contenteditable, locale_hint, timezone_hint, min, max, step,
  accept, multiple. Grabber's _controlMetadata extracts them.

- `795d75a` WI-04 idempotency-as-capability: removed global
  injection. PortalContext.idempotency carries enabled +
  header_name + endpoint_patterns + method_patterns +
  key_components + body_hash_fields + allow_existing_header.
  Runner gates injection on capability.enabled. JS shim reads
  window.__cp_idem_config and builds the key from declared
  components.

- `69521b5` WI-07 runner fail-fast: SkillRunner.run() honors
  step.replay_policy.on_failure. Default "abort" stops the loop
  on failure; "continue" / "optional" preserve legacy behavior
  for explicitly tolerant steps. 3 unit tests pin the behavior.

Tests: 22/22 pass.

## What I need from you

### 1. Verify each WI was actually implemented correctly

For each of WI-01, WI-02, WI-03, WI-04, WI-07: read the relevant
files and check that the implementation matches what the plan
specified. Specifically:

  - Schema additions are present and have the right shape.
  - Defaults preserve backwards compatibility for legacy skills.
  - The runner / grabber / annotator changes match what the WI's
    "Files to touch" and acceptance check spelled out.
  - No drift from the intent (e.g. WI-04 said "no global injection";
    confirm there isn't a fallback path that still always injects).

Read the actual files. Don't trust the commit messages alone.

### 2. Check for NEW hardcoded heuristics introduced

The original audit flagged 16 hardcoded heuristics. My implementation
of WI-01-04 + WI-07 must not have introduced new ones. Look for:

  - Magic timeouts / thresholds / counters in the new code
  - Implicit fallbacks that mask failures
  - Substring or regex patterns specific to one portal
  - Time-based dedupe windows
  - Buffer caps or list size limits without a clear justification
  - "Default to X" choices that should be configurable

If you find any, name them with file:line and propose a fix.

### 3. Check the new schema for design holes

The schema is large. Look for:

  - Fields that could be misused if left at default
  - Models where two fields conflict semantically (e.g. both
    `on_failure="abort"` AND `optional=True`)
  - Missing validation / constraints
  - Forward references that won't resolve
  - Fields that should be required but are optional, or vice versa

### 4. What's the minimum-set risk for landing the rest of the plan?

We deferred WI-05 (typed params) and WI-06 (diagnostics) from this
batch. Of the 43 remaining work items, which (if any) become
harder or impossible to do correctly because of choices we made
in WI-01-04, WI-07? Are there sequencing conflicts we should
re-think?

## Files

Code paths to read (under `D:\PROJECTS\Portal Assistant\Portal-assistant-bot`):

- `pilot/skill_models.py` (schema additions for WI-01, WI-02, WI-03)
- `pilot/agent/schemas/portal_context.py` (WI-04 IdempotencyCapability)
- `pilot/overlay/grabber.js` (WI-02 attribution + WI-03 control_metadata)
- `pilot/teach.py` (WI-02 passthrough)
- `pilot/annotate.py` (WI-02 causality graph + synthetic ids)
- `pilot/skill_runner.py` (WI-01 stubs, WI-04 idempotency JS,
  WI-07 fail-fast policy)
- `pilot/agent/executor_real.py` (WI-04 config plumbing)
- `pilot/agent/web_server.py` (WI-04 web wiring)
- `portals/sample_portal/context.yaml` (WI-04 capability declared)
- `tests/agent/test_trace_causality.py` (WI-02 unit tests)
- `tests/agent/test_fail_fast_policy.py` (WI-07 unit tests)

## Output

Structured report:

```
## Implementation correctness
[per WI: ok / drift / missing pieces, with citations]

## New hardcoded heuristics in WI-01-04, WI-07
[list with file:line + fix]

## Schema design issues
[list with concrete proposals]

## Sequencing risk for remaining 43 WIs
[which dependencies got harder / blocked / fine]
```

No time estimates. Concrete citations only. Skip generic praise.
