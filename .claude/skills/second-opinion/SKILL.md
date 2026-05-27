---
name: second-opinion
description: Use before committing to a non-trivial direction — architecture choices, large refactors, testing strategies, schema changes, anything you'd describe as "the plan." Writes the proposed approach to a spec file and sends it to a stronger external model via `droid exec` for adversarial review. Proceed only after each pushback is addressed, explicitly rejected with reason, or documented as known limitation.
---

# Second Opinion

The single most consequential class of mistake is committing to the wrong
direction. Bugs are cheap to fix; wrong architecture is expensive. This
skill plants a checkpoint at the exact moment where wrong assumptions
get baked in: just before you start the substantial work.

## When this fires

Trigger BEFORE substantive work, when at least one of the following is true:

- An architectural decision is on the table (new module, new schema,
  protocol shape, layering choice)
- A large refactor (>200 LOC OR touching ≥3 files in non-obvious ways)
- A testing-scope decision ("what's enough to call this verified?")
- A "should I do X or Y?" with real, non-symmetric tradeoffs
- A "pivot to a different approach" call

Do NOT trigger for:

- Bug fixes with a known root cause
- Single-function additions
- Mechanical refactors (rename, format, type-annotate)
- Anything the user has explicitly scoped down ("just X, nothing more")
- Anything where you've already consulted once on the same decision

## How to use it

1. **Write the spec to `tmp/<slug>-spec.md`** in the current project.
   Sections:
   - **Context** — what the system does, what's broken or missing,
     what failed or worked before
   - **Proposed approach** — the plan with explicit, named items in
     priority order
   - **Already considered and rejected** — alternatives and the
     specific reason for rejecting each
   - **Specific questions** — bullet list of things you want the
     reviewer to evaluate. Sharp questions ("is the ordering right?"
     "what failure class am I missing?") get sharp answers; vague
     questions ("does this look good?") get noise.

   Keep it under ~1500 words. The reviewer doesn't need the full
   codebase — it needs the model of the decision.

2. **Consult**:

   For a pure-architecture review where the spec contains all the
   context (no file reads needed):
   ```bash
   droid exec -m gpt-5.5 -f tmp/<slug>-spec.md --auto low
   ```

   For an audit / verification where the reviewer must read code
   from the repo to check claims (most code-review consults fall
   in this bucket):
   ```bash
   droid exec -m gpt-5.5 -f tmp/<slug>-spec.md --auto medium
   ```
   `--auto medium` lets the reviewer use Read / Grep / Glob. It
   does NOT permit code modifications -- the spec asks for a
   report, not a fix.

   For higher-stakes decisions, use `-m gpt-5.5-pro`. If
   `droid exec` isn't available, fall back to any reachable
   stronger model.

   If you forget which level to use, the runner will fail with
   "Exec ended early: insufficient permission to proceed" --
   re-run with `--auto medium`.

3. **Read the response carefully**. For each pushback, pick one of:
   - **Address**: change the plan, note in user-visible text what
     changed and why
   - **Reject with reason**: explicitly write down why you disagree
     and proceed anyway
   - **Defer**: document as known limitation; surface in the
     eventual summary so the operator can decide later

4. **Continue with the work**. Only after every pushback has a
   resolution.

5. **Save the trail**: after the sprint, move both files (spec +
   raw response) to `DOCS/reviews/<YYYY-MM-DD>_<slug>.md` so the
   rationale is auditable later.

## Writing the spec well

The reviewer is good but only as good as the spec. Tight specs that
get sharp pushback:

- Name your priorities explicitly: "do X first because it's the
  cheapest fix to the most concrete user pain"
- State what you're NOT doing: "I'm deliberately not chunking the
  trace into atomic skills yet because [reason]"
- Give the reviewer something to argue with: state opinions, don't
  just describe the design

Vague specs that get vague pushback:

- "We want to make the system better, here's some changes"
- "Is this a good architecture?"
- A bullet list of features with no reasoning

## Anti-patterns

- Using this as a rubber-stamp for a plan you're already committed
  to. That wastes the consult. If you're not willing to change
  course, don't ask.
- Pasting your entire codebase into the spec. The reviewer needs
  the model of the decision, not the implementation.
- Asking the same model that wrote the plan. The whole point is a
  fresh angle.
- Skipping the "address each pushback" step. Reading the response
  isn't the same as engaging with it.

## Cost

~30 seconds of latency and roughly $0.02-0.05 per consult. Cheap
relative to the cost of building the wrong thing. Use freely for
real decisions; don't use for trivial ones (the noise erodes the
signal).

## Brief output expectation

After the consult, your user-visible text should be:

```
Second opinion run on <slug>. Findings:
  - <pushback 1>: addressed by <change>
  - <pushback 2>: rejected because <reason>
  - <pushback 3>: deferred (will surface in summary)
Proceeding.
```

That's the receipt. Then start work.
