## Implementation correctness

### WI-01 — **mostly OK; one sidecar drift**

- **OK:** `ActionType` contains the new structured action types in `pilot/skill_models.py:41-54`.
- **OK:** `StepEffect` and effect submodels exist in `pilot/skill_models.py:305-426`.
- **OK:** `ReplayPolicy` exists with default `on_failure="abort"` in `pilot/skill_models.py:441-457`, and `SkillStep.replay_policy` defaults via `Field(default_factory=ReplayPolicy)` at `pilot/skill_models.py:645`.
- **OK:** `ParamProvenance` / `StepProvenance` exist at `pilot/skill_models.py:475-512`.
- **OK:** `Skill.schema_version` defaults to `1` for legacy loadability at `pilot/skill_models.py:709`.
- **OK:** runner stubs exist for unimplemented actions at `pilot/skill_runner.py:70`, dispatch at `pilot/skill_runner.py:321`, and fail with `error_kind="action_not_implemented"` at `pilot/skill_runner.py:570`.
- **Drift:** `pilot/agent/schemas/skill.py` was in the WI-01 plan, but v2 still stores `steps` as raw `list[dict[str, Any]]` at `pilot/agent/schemas/skill.py:107`; it does not validate/express the new structural step/effect semantics.

### WI-02 — **partial; important causality drift remains**

- **OK:** `TraceEvent` has `event_id`, `interaction_id`, `caused_by`, `sequence`, `source`, `monotonic_ts`, `raw_event_kind`, `page_state_before`, and `page_state_after` at `pilot/skill_models.py:803-839`.
- **OK:** teach passes those fields through at `pilot/teach.py:220-228`.
- **OK:** annotator assigns synthetic IDs at `pilot/annotate.py:71` and builds a graph at `pilot/annotate.py:99`.
- **Drift:** `run_annotate()` filters raw events before `build_skill()` ever builds the causality graph: `pilot/annotate.py:536`. The filter still drops same-URL navigations at `pilot/annotate.py:146-150`, empty clears at `pilot/annotate.py:155`, and duplicate clicks using `<0.2s` at `pilot/annotate.py:169`.
- **Missing:** grabber does not populate `page_state_before` / `page_state_after`; click posts only `kind`, `fingerprint`, `page_url`, `raw_event_kind` at `pilot/overlay/grabber.js:729-734`, and navigation posts only `kind`, `url`, `page_url`, `raw_event_kind` at `pilot/overlay/grabber.js:1019-1026`.
- **Missing:** request observations and DOM mutations are not emitted as trace events. Fetch/XHR hooks only maintain counters at `pilot/overlay/grabber.js:186-207` and `pilot/overlay/grabber.js:215-225`.

### WI-03 — **schema/extraction present; select metadata currently breaks validation**

- **OK:** `ElementFingerprint` has the new control metadata fields at `pilot/skill_models.py:99-161`.
- **OK:** grabber adds control metadata into fingerprints at `pilot/overlay/grabber.js:420-456`, with extraction starting at `pilot/overlay/grabber.js:467`.
- **Bug:** `options_snapshot` is typed as `list[dict[str, str]]` at `pilot/skill_models.py:121`, but grabber writes boolean `selected=true` at `pilot/overlay/grabber.js:631`. Pydantic rejects this shape, and teach silently downgrades the whole fingerprint to `None` at `pilot/teach.py:203-206`.
  - **Fix:** add an `OptionSnapshot` model, e.g. `value: str`, `label: str`, `selected: bool = False`, and use `list[OptionSnapshot]`.
- **Drift:** ARIA combobox/listbox gets only `control_kind` / `value_kind` at `pilot/overlay/grabber.js:648-653`; no ARIA option snapshot is captured there.

### WI-04 — **core opt-in wiring OK; sample retry safety is wrong**

- **OK:** `IdempotencyCapability` shape is present at `pilot/agent/schemas/portal_context.py:48-104`.
- **OK:** default is safe-disabled via `enabled: bool = False` at `pilot/agent/schemas/portal_context.py:60`.
- **OK:** executor config carries idempotency at `pilot/agent/executor_real.py:104`, wires it into runner at `pilot/agent/executor_real.py:450`, and web server sources it from `PortalContext` at `pilot/agent/web_server.py:853-854`.
- **OK:** runner only installs the idempotency shim when `_idempotency_enabled()` is true at `pilot/skill_runner.py:1477-1479`; disabled mode clears stale config at `pilot/skill_runner.py:1675`.
- **Drift:** sample portal retry dedupe will not use the runner’s stable key. The app generates a fresh key per mutation at `sample_portal/src/lib/api.js:32` and `sample_portal/src/pages/AssetDetail.jsx:113,130`; runner preserves existing headers when `allow_existing_header` is true at `pilot/skill_runner.py:1600`; sample context sets `allow_existing_header: true` at `portals/sample_portal/context.yaml:152`.
  - **Fix:** set sample `allow_existing_header: false`, or remove app-generated per-call keys for replay-covered mutations.
- **Bug:** XHR existing-header detection hardcodes only `"idempotency-key"` at `pilot/skill_runner.py:1627`, ignoring configured `header_name`.
- **Bug:** fetch wrapping ignores `Request` input headers by calling `_augment(init.headers || {}, ...)` at `pilot/skill_runner.py:1614`.
  - **Fix:** seed from `input.headers` when `input instanceof Request`.

### WI-07 — **OK**

- **OK:** default `on_failure="abort"` is in schema at `pilot/skill_models.py:442`.
- **OK:** runner computes effective policy from `optional` / `on_failure` at `pilot/skill_runner.py:220-222` and breaks for `"abort"` / `"recover"` at `pilot/skill_runner.py:223-235`.
- **OK:** tests pin abort/continue/optional at `tests/agent/test_fail_fast_policy.py:55`, `tests/agent/test_fail_fast_policy.py:83`, and `tests/agent/test_fail_fast_policy.py:106`.

**Validation note:** targeted WI tests pass: `test_trace_causality.py` + `test_fail_fast_policy.py` = `7 passed`. Full `tests/agent` currently fails `1/24`: local override `OneTimeFailExecutor.execute()` lacks `sub_step_overrides` at `tests/agent/test_integration_multi_item.py:124`, while retry passes it at `pilot/agent/orchestrator.py:891-892`; assertion fails at `tests/agent/test_integration_multi_item.py:187`.

## New hardcoded heuristics in WI-01-04, WI-07

- `pilot/overlay/grabber.js:43` / `pilot/overlay/grabber.js:82` — new `ATTRIBUTION_WINDOW_MS = 3000` time-window causality.
  - **Fix:** replace with explicit causal tokens from wrapped operations, or make attribution scope/config explicit rather than time-based.
- `pilot/overlay/grabber.js:625` — select option snapshot cap `opts.length < 200` with no truncation marker.
  - **Fix:** capture all options, or add `options_truncated: true` plus a configurable cap.
- `pilot/skill_models.py:249`, `pilot/skill_models.py:263`, `pilot/skill_models.py:267`, `pilot/skill_models.py:335` — new schema defaults for wait/stability/navigation timeouts.
  - **Fix:** move defaults into `PortalContext` / future `WaitPolicy`, or require explicit values on generated expectations.
- `pilot/agent/schemas/portal_context.py:69-72` + `pilot/skill_runner.py:1581` — when idempotency is enabled and `endpoint_patterns` is empty, shim injects into all configured non-GET methods.
  - **Fix:** require non-empty `endpoint_patterns` when `enabled=True`, unless an explicit `scope_all_endpoints: true` is declared.
- `pilot/skill_runner.py:1616-1617` and `pilot/skill_runner.py:1647` — idempotency shim fails open on exceptions.
  - **Fix:** emit structured diagnostics or fail closed for matched destructive requests when idempotency is enabled.
- `sample_portal/mock_backend.js:201` — sample backend uses a fixed `60_000ms` idempotency dedupe window.
  - **Fix:** make it a named/configured constant in the fixture so tests don’t encode a hidden retry assumption.

## Schema design issues

- `options_snapshot` type conflicts with producer: `pilot/skill_models.py:121` vs `pilot/overlay/grabber.js:631`.
  - **Proposal:** replace with typed `OptionSnapshot(value, label, selected, disabled?)`.
- `ReplayPolicy` allows contradictory states: `on_failure="abort"` plus `optional=True` at `pilot/skill_models.py:442` and `pilot/skill_models.py:457`.
  - **Proposal:** model-validator should normalize or reject contradictions.
- `TraceEvent.kind` is too narrow for planned WI-02 consequences: only `click/input_change/submit/file_selected/navigate/key` at `pilot/skill_models.py:782-789`.
  - **Proposal:** add raw kinds for `network`, `dom_mutation`, `popup`, `download`, or a separate raw-event envelope.
- `ParamProvenance.confidence` documents `0.0-1.0` but has no constraint at `pilot/skill_models.py:493`.
  - **Proposal:** `Field(default=None, ge=0.0, le=1.0)`.
- `NetworkExpectation.status=None` doc says “any 2xx” at `pilot/skill_models.py:246-249`, but runner accepts any status when `statusFilter === null` at `pilot/skill_runner.py:1844-1849`.
  - **Proposal:** enforce `200 <= status < 300` when status is omitted.
- `ExpectedSignals.network` has no baseline/event scope; matching can be satisfied by stale request log entries at `pilot/skill_runner.py:1844-1849`.
  - **Proposal:** add WI-09 fields now: `started_after_event`, `baseline`, or `request_id`.

## Sequencing risk for remaining 43 WIs

- **WI-05 / WI-17 / WI-18 are blocked until WI-03 select metadata is fixed.** Current selected native selects can lose the entire fingerprint because of the `options_snapshot.selected` validation mismatch.
- **WI-08 / WI-12 / WI-14 become harder if `filter_events()` remains ahead of graph construction.** Same-URL navigations and duplicate clicks are discarded before causality/provenance can reason about them.
- **WI-09 / WI-10 / WI-43 / WI-47 need request/DOM observations beyond counters.** Current grabber does not emit request IDs, DOM mutation events, or action-scoped baselines.
- **WI-11 should not build on current substring templating.** Legacy substring templating remains in `pilot/annotate.py:361-404`; provenance fields exist but are not used yet.
- **WI-35 / WI-45 / WI-46 need TraceEvent kind expansion.** Popup/download/cross-tab evidence cannot fit cleanly in the current `TraceEvent.kind` literal.
- **WI-06 should move earlier than planned.** Silent fingerprint validation drops at `pilot/teach.py:203-206` and idempotency fail-open paths at `pilot/skill_runner.py:1616-1617` will make the next batch hard to debug.
- **WI-04 should be corrected before more destructive replay work.** Sample portal’s `allow_existing_header: true` conflicts with per-call app keys, so retry safety is not actually proven for mutations.
