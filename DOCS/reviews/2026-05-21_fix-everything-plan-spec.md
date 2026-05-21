# CurationPilot — Concrete Implementation Plan Covering EVERY Audit Finding

## Context

You (GPT 5.5) recently audited the teach / annotate / replay code
paths of CurationPilot and produced a thorough findings report.
Both documents are available; read them in full before responding:

- **The original audit spec** (background + end goal + interaction
  types to cover):
  `D:\PROJECTS\Portal Assistant\Portal-assistant-bot\tmp\teach-replay-deep-audit-spec.md`

- **Your prior audit response** (BLOCKERS / MAJOR / MINOR /
  STRATEGIC findings + 17-interaction dry-run matrix + "what was
  missed" list + "Fix First" ordering):
  `D:\PROJECTS\Portal Assistant\Portal-assistant-bot\tmp\teach-replay-deep-audit-response.md`

The operator has decided: **we are fixing every single finding.**
Not picking. Not deferring. Every BLOCKER, every MAJOR, every
MINOR, every STRATEGIC item, every row in the 17-interaction
matrix, every item in the "what you missed" list.

The operator's quote: "We wont pick what to fix due to time, We
will fix every single thing. no matter what time it would take."

Your job now: produce a concrete, sequenced implementation plan to
do that, with reasoning for the sequencing.

## What I need from you in this response

A complete plan that covers ALL findings from the prior audit, NOT
just the BLOCKERS or the "Fix First" five. Every item.

### Structure

For each work item:

```
WI-NN. <one-line title>
  Source: <audit finding citation — e.g. "BLOCKER 1", "MAJOR row
    'Select fuzzy threshold 0.7'", "STRATEGIC item 3", "17-matrix
    row 8 (Slider)", "What You Missed: Hover menus">
  Why this ordering: <one paragraph: what predecessors must land
    first, what successors block on this. The reasoning is the
    point.>
  Files to touch:
    - <path:lines or path>
    - ...
  Skill schema changes:
    - <field add / rename / removal>
  Grabber changes:
    - <what events change, what's added>
  Annotator changes:
    - <what's coalesced / detected / templated>
  Runner changes:
    - <what's verified / waited for / executed>
  Migration: <does it break existing recordings? if yes, how to
    transition them>
  Acceptance check: <how to know it's done — a concrete test or
    measurable outcome>
```

### Sequencing constraint

Order items so that dependencies are respected. Where item B
depends on item A (e.g. B needs a schema field that A introduces),
A must come first. Where two items are independent, group them so
they can land together.

### NO TIME ESTIMATES

The operator has explicitly and repeatedly instructed: do not
include time estimates of any kind. Not "small", not "large", not
"this week", not "1 day", not "quick win". Just the work, the
reasoning, and the order.

### Reasoning is the point

Don't just list. For every item, explain:
- Why is it sequenced where it is
- What design choice you're making and what alternative you
  considered + rejected
- What real-world failure mode it prevents

A 100-line plan with no reasoning is useless. A 1000-line plan
with sharp reasoning is exactly what we need.

## Specific things to cover

You must produce a work item for at least each of the following
(from your prior audit):

### BLOCKERS
1. Click-caused SPA navigation modeled as independent navigate
2. expected_signals never populated; runner waits before action
3. Input bursts + widget workflows not collapsed into semantic actions
4. Waits can falsely satisfy while page not ready (the 5 race points)
5. Runner continues after sub-step failure

### MAJOR — Hardcoded heuristic audit (every row in the table)
6. Consecutive same-URL navigate dedupe uses `last_nav_url`
7. Empty input after prior same target is dropped
8. Duplicate click `<200ms` dropped
9. Param inference only for `input_change` / `file_selected`
10. Template derivation uses substring replace, `len >= 3`
11. Navigate URL is never templated
12. Select fuzzy threshold `0.7`
13. Ambiguity only fires for templated `test_id` / `element_id`
14. `_first_visible` catches all exceptions then uses `count() > 0`
15. CSS escape only handles `\` and `'`
16. Generic settle numbers (250ms quiet, 2s DOM, 4s total, 1.5s spinner)
17. Spinner selector list is convention-based
18. Request log cap 50
19. L3 repair thresholds 0.85/0.65/0.55
20. L3 verification uses URL/text length/interactable count
21. `page.goto(...networkidle...)`, then second goto on timeout
22. Idempotency key strips query/hash only

### MINOR
23. Silent failures (7 specific sites cited in your audit)
24. Schema-ahead-of-implementation: frame_path / in_shadow_root

### STRATEGIC
25. Grabber deciding semantic units too early (text debounce)
26. Annotator templates via substring luck (param provenance instead)
27. Runner re-deriving semantics that should be data
28. Idempotency injection too global (portal capability data instead)
29. LLM annotate enriches names, not structure

### 17-interaction matrix (every row)
30. Text + Enter submit
31. Search + dropdown results
32. Native single select
33. Cascading native selects
34. Custom multi-select
35. Nested dropdown multi-select
36. Date picker
37. Slider
38. File upload
39. Drag / drop
40. Accordion / collapse panels
41. Modal open / close
42. Click that navigates
43. Click that opens popup / new window
44. Auth-required workflows
45. Virtualized / paginated tables
46. Scroll

### What you missed (every item)
47. contenteditable / rich text editors
48. Iframes and shadow DOM
49. Hover menus and mega-menus
50. Global keyboard shortcuts
51. Toast-driven undo / conflict-resolution flows
52. Disabled-to-enabled readiness
53. Server validation errors bound to fields
54. Download / export workflows
55. Cross-tab workflows
56. WebSocket / SSE-driven state changes
57. Locale / timezone-sensitive dates and numbers
58. Canvas / SVG / media timeline controls

That's roughly 58 work items by my count. If any of them collapses
naturally into another (e.g. "click that navigates" + "click +
caused navigation" share the same fix), say so and produce one
combined work item with both source citations.

If new work items emerge from cross-referencing items (e.g.
fixing #1 requires also doing a schema change that other items
depend on), surface them as additional WI-NNs.

## Pushback expected

Engage adversarially. Specifically:

- Where the audit's previous recommendation was wrong or
  incomplete, say so and propose better.
- Where two items are coupled in ways that would create rework if
  done independently, say so and merge them.
- Where an item is actually a non-issue (you flagged it under
  pressure but on reflection it isn't worth fixing), say so and
  explain.
- Where the operator's end goal (from the original spec) requires
  work items you DIDN'T flag in the audit, add them.

Do not perform a generic "AGREE with all prior findings."

## Output

Single markdown document. Sections by work-item-bucket if helpful
(schema-first, grabber, annotator, runner, orchestrator). Each
work item formatted as specified above.

No time estimates anywhere.
