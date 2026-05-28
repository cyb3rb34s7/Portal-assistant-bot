# CurationPilot vs. Microsoft Webwright — Architecture Comparison

**Purpose:** Evaluate whether we should adopt Microsoft's Webwright for our enterprise portal automation, or continue with our in-house CurationPilot approach.

**Bottom line:** Webwright and CurationPilot solve *different* problems. Webwright is an autonomous, goal-driven general web agent. CurationPilot is a governed, demonstration-driven automation platform for a specific enterprise portal. For our requirements — a write-heavy OTT content-curation portal, batch execution, drift survival across portal releases, an audit trail, and zero tolerance for unintended actions — CurationPilot is the better-fit architecture, and adopting Webwright wholesale would be a downgrade on the dimensions we care about most. Webwright still has ideas worth borrowing, listed at the end.

> Scope note: "better" here means *better for our use case*, not universally better. Webwright is an excellent tool for the problem it was built for.

---

## The core architectural difference

**Webwright is goal-driven and LLM-in-the-loop.** You give it a natural-language task plus a start URL; an LLM writes Playwright code to accomplish the goal, runs it in a terminal/workspace, observes the result, and iterates. There is no human demonstration — the model figures out the workflow itself. The artifact is a standalone parameterized Python script.

**CurationPilot is demonstration-driven and deterministic-first.** An operator performs the task once; we passively record it; an LLM annotates the trace *offline* into a parameterized, versioned skill; and we replay it as deterministic code, using the LLM *online* only as a high-level planner and as a diagnostician when something breaks. The artifact is a versioned skill executed by our runner with a four-level locator fallback.

That single difference — *the model authors the workflow* vs. *a human demonstrates it and the system replays it* — drives everything below.

---

## Side-by-side

| Dimension | CurationPilot (ours) | Webwright (Microsoft) |
|---|---|---|
| **Primary paradigm** | Record → annotate → replay (demonstration-driven) | Goal → author → run (autonomous agent) |
| **Human recording** | Native: framework-agnostic passive CDP listener capturing clicks, fills, navigation, network, SSE/WS, DOM mutations, shadow DOM, iframes, with causality links | None — the model never watches a human |
| **Execution model** | Deterministic compiled steps; LLM not in the per-step loop | LLM-in-the-loop, authors/runs code each turn |
| **Speed at batch scale** | Machine speed; no per-step token cost | Slower; model reasons per step |
| **LLM cost profile** | LLM used offline (annotation) + online only for planning/diagnosis | LLM used continuously during execution |
| **Drift resilience** | 4-level fallback (exact → semantic → self-heal w/ confidence bands → human), persists healed locators as alternates so knowledge accumulates | Re-derives the workflow each run; no persistent per-portal locator memory |
| **Element targeting** | Rich fingerprints: testid, id, aria, role, accessible name, text, css path, xpath, landmark, ancestor chain, bbox | ARIA snapshot + screenshot read fresh each run |
| **Governance / safety** | Auth pause, plan-approval gate, destructive-action gating (publish/delete require approval), human-in-the-loop recovery before any patch | Self-verification of task success; not a governance layer |
| **Hallucination control** | Deterministic annotator is structural truth; LLM overlays are advisory and validated against trace evidence (unbacked claims dropped + audited) | Model-authored code is the workflow; correctness checked by post-hoc verification |
| **Audit trail** | Per-session JSONL event log + before/after screenshots + API verification | Logs + screenshots + cumulative script |
| **Artifact** | Versioned skill (JSON), runner-executed; down-convertible to Puppeteer Replay | Standalone, import-safe Python CLI tool (function + docstring + argparse) |
| **LLM provider** | Provider-agnostic (Bedrock, OpenAI, Groq, custom org, mock) — on-prem friendly | Host-agent driven (Claude Code / Codex / etc.) |
| **Maturity** | Internal POC, end-to-end verified on sample portal | Microsoft Research release with ecosystem integrations and a published benchmark |
| **Generality** | Tuned to our portal pattern | General-purpose across arbitrary sites |

---

## Why ours is the better fit for this use case

**1. We need the recording layer Webwright doesn't have.** Our entire premise is "learn the task by watching the operator." Our capture layer is a framework-agnostic passive listener that records not just clicks and fills but network requests, SSE/WebSocket messages, DOM mutations, shadow DOM, iframes, and readiness transitions — with causality links (a click that triggers a route change is recorded as two linked events). Webwright has no equivalent; it would have to be rebuilt from scratch to support our workflow. This is a structural gap, not a feature gap.

**2. Deterministic execution gives us speed, cost, and reproducibility.** Because we replay compiled steps rather than asking a model what to do at each step, a multi-hour human workflow runs in minutes, with no per-step token cost, and the deterministic core is byte-for-byte reproducible. Webwright pays LLM latency and tokens on every step, which does not scale well to a batch of dozens or hundreds of assets.

**3. Drift resilience that accumulates knowledge.** When a portal release renames a `data-testid` or moves a button, our four-level fallback recovers — exact match, then semantic match, then a confidence-scored self-heal (with a structural policy gate that refuses risky matches), then human takeover — and it *persists* the healed locator back onto the skill so the next run is fast again. Webwright re-derives the workflow each run and accumulates no per-portal memory.

**4. Governance is built in — this is our answer to "we can't afford wrong clicks."** Our orchestrator pauses for authentication (SSO/2FA), requires plan approval, gates destructive actions (publish/delete need explicit approval even in auto-run; reversible saves don't), and on failure proposes a fix that a human must approve before any change is applied. Webwright's self-reflection verifies *task success*; it is not a governance layer and does not gate irreversible actions behind human approval. For a portal that touches live content, this difference is decisive.

**5. Disciplined use of the LLM.** We keep the model out of the deterministic execution path and constrain it where we do use it: the deterministic annotator produces structural truth, and the LLM enrichment pass can only *add* advisory metadata that is validated against the recorded trace — unsupported claims are dropped and logged, not silently persisted. This is a more conservative posture than letting a model author the executable workflow directly.

**6. Enterprise integration realities.** Our provider-agnostic AI client (Bedrock, OpenAI, Groq, custom org endpoints) fits enterprise procurement and on-prem constraints, and our per-session audit artifacts support compliance review. Webwright is designed to be driven by a host coding agent.

---

## Where Webwright is genuinely better (fair credit)

A balanced evaluation has to acknowledge these:

- **Artifact portability.** Webwright's output is a standalone, readable, git-diffable Python script. Our skill is JSON interpreted by our runner, which is harder for an outside engineer to review in isolation. *(Mitigation: we can add a compiler that emits a guarded standalone script from our skill schema — see below.)*
- **Cold-start authoring.** For a brand-new task with no recording, Webwright can draft a workflow from a goal. We require a human to teach it first. *(For us this is acceptable — teaching is fast and safe, and an autonomous explorer is too risky on a write-heavy prod portal.)*
- **Simplicity and maintenance cost.** Webwright is deliberately minimal (a ~450-line agent loop). Our system carries far more surface area (a large capture script, dozens of modules, an extensive test matrix). That breadth is justified by what we do, but it is a real maintenance and onboarding cost.
- **Maturity and backing.** Webwright is a Microsoft Research release with a published benchmark result and ecosystem integrations. CurationPilot is an internal POC; it is verified end-to-end on a sample portal but has not yet been hardened against a real OTT portal at scale.

---

## Honest risks in our own approach (for transparency)

- **The self-heal confidence bands are still heuristic.** They recover from corrupted-locator test fixtures today, but they need validation against a *real* portal release where components are re-themed or restructured, not just synthetic drift.
- **Single sample portal so far.** Real enterprise auth, rate limits, dynamic re-renders, and deeply nested iframes/shadow DOM will stress the capture and replay layers in ways the demo portal does not.
- **Surface area.** The breadth that makes us capable also makes us harder to maintain; we should keep the complexity earning its keep.

---

## Recommendation

Continue with CurationPilot as the production path. It is the right architecture for a governed, drift-resilient, auditable, batch-capable automation platform on a write-heavy enterprise portal — and it does the two things Webwright structurally cannot: learn from a human demonstration, and execute deterministically under a human-approval governance model.

Borrow selectively from Webwright rather than adopting it:

1. **A skill-to-standalone-script compiler** — emit a portable, git-diffable Playwright script from our skill schema, preserving our tiered locators and compiling destructive actions into explicit confirmation gates. Improves reviewability and portability.
2. **A screenshot-grounded promotion gate** — adopt Webwright's "prove each success criterion against a screenshot before declaring done" pattern as the acceptance test that promotes a freshly taught skill to "trusted."

We considered, and recommend against, an autonomous exploration agent: on a write-heavy portal its safe (read-only) form can't map workflows that only reveal structure after a mutating step, while its useful form risks unintended actions — a poor trade when teaching already covers cold start safely.
