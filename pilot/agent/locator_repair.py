"""Locator repair (L3 self-heal) for the deterministic skill_runner.

The deterministic core (L1 + L2) is byte-for-byte reproducible and
covers >95% of steps on stable enterprise portals. When both fail —
typically because a portal release renamed a `data-testid` or restructured
a button — L3 finds an alternate element that matches the recorded
intent, with a confidence score the runner uses to decide whether to
execute, ask for human takeover, or refuse outright.

Two backends, same interface:

  - **LLM backend** (when an ``AIClient`` is provided): distills the
    page's interactable elements, builds a prompt that names the
    operator-recorded label + the original fingerprint, asks the model
    to pick by index from the distilled list. Confidence comes from
    the model.

  - **Deterministic backend** (default; used when no AIClient is
    configured): the same weighted similarity scorer the runner used
    before this module existed. Confidence is mapped from the score
    so the rest of the system has one shape regardless of backend.

Either way, the result is a ``HealResult`` with a Playwright locator,
a confidence band, a one-sentence reason, and a fresh
``ElementFingerprint`` describing what the runner actually clicked.
The runner persists that fingerprint as an *alternate* on the step so
the next replay hits L1.

Safety policy (enforced by the runner, not this module):
  - confidence == "low"  -> refuse to execute, escalate to human (L4)
  - confidence == "medium" -> execute, then verify post-condition;
        do NOT persist alternate even on success
  - confidence == "high" -> execute, verify, persist on success
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from playwright.sync_api import Locator, Page

from pilot.skill_models import ElementFingerprint


Confidence = Literal["high", "medium", "low"]


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class HealResult:
    locator: Locator | None
    """Playwright locator the runner should use, or None if no candidate
    cleared the safety threshold (caller should escalate to L4)."""

    confidence: Confidence
    reason: str
    """One-sentence justification — the LLM's reasoning, or
    'deterministic best-match score=0.78' for the deterministic path."""

    new_fingerprint: ElementFingerprint | None
    """Full fingerprint of the picked element, suitable for persisting
    onto the step's ``alternates`` list. None when ``locator`` is None."""

    backend: Literal["llm", "deterministic"] = "deterministic"


# ---------------------------------------------------------------------------
# JS distillation snippet — shared across backends
# ---------------------------------------------------------------------------


_JS_LIST_INTERACTABLES = """
(() => {
  const out = [];
  const selector = 'button, a, input, select, textarea, [role], [data-testid], [onclick]';
  const nodes = Array.from(document.querySelectorAll(selector));
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const pathParts = [];
    let node = el;
    while (node && node.nodeType === 1 && pathParts.length < 12) {
      let seg = node.nodeName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sameTag = Array.from(parent.children).filter(
          (c) => c.nodeName === node.nodeName
        );
        if (sameTag.length > 1) seg += '[' + (sameTag.indexOf(node) + 1) + ']';
      }
      pathParts.unshift(seg);
      node = parent;
    }
    const xpath = '/' + pathParts.join('/');
    let landmark = null;
    let cur = el.parentElement;
    while (cur) {
      const tid = cur.getAttribute && cur.getAttribute('data-testid');
      if (tid && /(page|panel|modal|dialog)/i.test(tid)) { landmark = tid; break; }
      cur = cur.parentElement;
    }
    out.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      testId: el.getAttribute('data-testid'),
      role: el.getAttribute('role') || null,
      ariaLabel: el.getAttribute('aria-label') || null,
      accessibleName: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 120),
      text: (el.innerText || el.textContent || '').trim().slice(0, 120),
      placeholder: el.getAttribute('placeholder'),
      inputType: el.tagName.toLowerCase() === 'input' ? (el.getAttribute('type') || 'text') : null,
      landmark,
      bbox: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      xpath,
    });
  }
  return out;
})()
"""


# ---------------------------------------------------------------------------
# Deterministic backend
# ---------------------------------------------------------------------------


def _similarity(fp: ElementFingerprint, cand: dict[str, Any]) -> float:
    """Weighted similarity of a candidate to the original fingerprint.
    Same scorer as the prior _level3 in skill_runner."""
    score = 0.0
    weights_total = 0.0

    def add(weight: float, matcher: float) -> None:
        nonlocal score, weights_total
        score += weight * matcher
        weights_total += weight

    if fp.test_id and cand.get("testId"):
        add(3.0, 1.0 if fp.test_id == cand["testId"] else 0.0)
    if fp.element_id and cand.get("id"):
        add(2.0, 1.0 if fp.element_id == cand["id"] else 0.0)
    if fp.role and cand.get("role"):
        add(1.5, 1.0 if fp.role == cand["role"] else 0.0)
    if fp.tag and cand.get("tag"):
        add(0.5, 1.0 if fp.tag == cand["tag"] else 0.0)
    if fp.accessible_name and cand.get("accessibleName"):
        add(
            2.5,
            difflib.SequenceMatcher(
                None,
                fp.accessible_name.lower(),
                (cand["accessibleName"] or "").lower(),
            ).ratio(),
        )
    if fp.text and cand.get("text"):
        add(
            1.0,
            difflib.SequenceMatcher(
                None,
                fp.text.lower()[:60],
                (cand["text"] or "").lower()[:60],
            ).ratio(),
        )
    if fp.placeholder and cand.get("placeholder"):
        add(1.5, 1.0 if fp.placeholder == cand.get("placeholder") else 0.0)
    if fp.landmark and cand.get("landmark"):
        add(1.0, 1.0 if fp.landmark == cand["landmark"] else 0.0)

    if weights_total == 0:
        return 0.0
    return score / weights_total


# Score thresholds for the deterministic backend's confidence mapping.
# These are tuned by inspection: 0.85+ is near-identical (likely just a
# class change), 0.65+ is a recognizable sibling (label match + role
# match), below 0.55 is too uncertain to risk.
_DET_HIGH = 0.85
_DET_MEDIUM = 0.65
_DET_REFUSE_BELOW = 0.55


def _confidence_from_score(score: float) -> Confidence | None:
    if score >= _DET_HIGH:
        return "high"
    if score >= _DET_MEDIUM:
        return "medium"
    if score >= _DET_REFUSE_BELOW:
        return "low"
    return None


def _candidate_to_fingerprint(c: dict[str, Any]) -> ElementFingerprint:
    return ElementFingerprint(
        test_id=c.get("testId"),
        element_id=c.get("id"),
        aria_label=c.get("ariaLabel"),
        role=c.get("role"),
        accessible_name=c.get("accessibleName"),
        text=c.get("text"),
        placeholder=c.get("placeholder"),
        tag=c.get("tag"),
        input_type=c.get("inputType"),
        xpath=c.get("xpath"),
        landmark=c.get("landmark"),
        bbox=c.get("bbox"),
    )


def _heal_deterministic(
    page: Page, original_fp: ElementFingerprint
) -> HealResult:
    """Best-match score across all interactables. Falls back to refuse
    when nothing clears _DET_REFUSE_BELOW."""
    try:
        candidates = page.evaluate(_JS_LIST_INTERACTABLES)
    except Exception as e:  # noqa: BLE001
        return HealResult(
            locator=None,
            confidence="low",
            reason=f"could not enumerate interactables: {type(e).__name__}: {e}",
            new_fingerprint=None,
            backend="deterministic",
        )
    if not candidates:
        return HealResult(
            locator=None,
            confidence="low",
            reason="page has no interactable elements",
            new_fingerprint=None,
            backend="deterministic",
        )

    best_score = 0.0
    best_cand: dict[str, Any] | None = None
    for c in candidates:
        s = _similarity(original_fp, c)
        if s > best_score:
            best_score = s
            best_cand = c

    confidence = _confidence_from_score(best_score)
    if confidence is None or best_cand is None:
        return HealResult(
            locator=None,
            confidence="low",
            reason=(
                f"no candidate cleared the threshold "
                f"(best score={best_score:.2f})"
            ),
            new_fingerprint=None,
            backend="deterministic",
        )

    xpath = best_cand.get("xpath")
    if not xpath:
        return HealResult(
            locator=None,
            confidence="low",
            reason="best-match candidate had no xpath",
            new_fingerprint=None,
            backend="deterministic",
        )

    try:
        loc = page.locator(f"xpath={xpath}").first
    except Exception as e:  # noqa: BLE001
        return HealResult(
            locator=None,
            confidence="low",
            reason=f"could not build locator from xpath: {e}",
            new_fingerprint=None,
            backend="deterministic",
        )

    label_summary = (
        best_cand.get("testId")
        or best_cand.get("accessibleName")
        or best_cand.get("text")
        or best_cand.get("tag")
        or "?"
    )
    return HealResult(
        locator=loc,
        confidence=confidence,
        reason=(
            f"deterministic best-match: '{label_summary}' "
            f"(similarity={best_score:.2f})"
        ),
        new_fingerprint=_candidate_to_fingerprint(best_cand),
        backend="deterministic",
    )


# ---------------------------------------------------------------------------
# LLM backend (used when an AIClient is wired in)
# ---------------------------------------------------------------------------


class _LlmHealOutput(BaseModel):
    """Schema we ask the model to return.

    Index is into the candidates list (0-based). -1 means refuse.
    """

    element_index: int = Field(
        description=(
            "Index of the chosen element in the provided candidate list. "
            "Use -1 to refuse if no candidate is a confident match."
        )
    )
    action: Literal["click", "fill", "select"] = Field(
        description="What action best matches the operator's recorded intent."
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Your own confidence in the pick."
    )
    reason: str = Field(
        description="One short sentence justifying the pick.",
        max_length=240,
    )


_LLM_SYSTEM_PROMPT = """\
You are the L3 locator-repair stage of a portal automation system.

The operator recorded a workflow against a portal. On replay, the
recorded element could not be found via deterministic locators (testid,
id, aria, role+name). You are given:

  1. A short description of the operator's intent (the recorded label).
  2. The original fingerprint of the element they clicked.
  3. The current page's interactable elements as a numbered list.

Pick the candidate that best matches the recorded intent. Rules:

- If no candidate is plausibly the right element, set element_index=-1.
  Do not guess.
- Prefer matches by accessible_name/text + role over visual position.
- The operator's recorded label is your strongest signal.
- Confidence:
    high = the candidate clearly matches the intent (e.g. same role +
           accessible name within 1-2 word difference).
    medium = plausible but not certain — there is one nearby element
           that could also be the right one.
    low = a guess; you would not bet on it. (Caller will refuse.)

Return STRICT JSON matching the schema. No prose around it.
"""


def _heal_with_llm(
    page: Page,
    original_fp: ElementFingerprint,
    semantic_label: str | None,
    *,
    client: Any,
    model: str | None,
    complete_structured: Any,
    Message: Any,
) -> HealResult:
    """LLM-backed healing. The complete_structured callable + Message
    type are injected to keep this module independent of pilot.agent
    internals; the runner wires them in via _make_repair()."""
    try:
        candidates = page.evaluate(_JS_LIST_INTERACTABLES)
    except Exception as e:  # noqa: BLE001
        return HealResult(
            locator=None, confidence="low",
            reason=f"could not enumerate interactables: {e}",
            new_fingerprint=None, backend="llm",
        )
    if not candidates:
        return HealResult(
            locator=None, confidence="low",
            reason="no interactables to choose from",
            new_fingerprint=None, backend="llm",
        )

    # Compact list for the prompt — drop bbox/xpath the model doesn't need.
    distilled = []
    for i, c in enumerate(candidates):
        distilled.append({
            "i": i,
            "tag": c.get("tag"),
            "role": c.get("role"),
            "name": c.get("accessibleName"),
            "text": (c.get("text") or "")[:80],
            "testId": c.get("testId"),
            "placeholder": c.get("placeholder"),
            "landmark": c.get("landmark"),
        })

    user_prompt = (
        "Operator's intent (recorded label): "
        f"{semantic_label or '(none recorded)'}\n\n"
        "Original element fingerprint:\n"
        f"  testid={original_fp.test_id!r}\n"
        f"  id={original_fp.element_id!r}\n"
        f"  role={original_fp.role!r}\n"
        f"  accessible_name={original_fp.accessible_name!r}\n"
        f"  text={(original_fp.text or '')[:80]!r}\n"
        f"  tag={original_fp.tag!r}\n"
        f"  landmark={original_fp.landmark!r}\n\n"
        "Current page candidates (pick one by index, or -1 to refuse):\n"
        + json.dumps(distilled, indent=1)
    )

    try:
        # complete_structured is async; the caller awaits this function
        # in an async path. Here we deliberately keep the function sync
        # by accepting the call shape directly. See _make_repair() in
        # the runner for how this is plumbed without leaking asyncio
        # into the runner thread.
        decision = complete_structured(
            client,
            messages=[
                Message(role="system", content=_LLM_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_model=_LlmHealOutput,
            model=model,
            temperature=0.0,
            max_retries=2,
        )
    except Exception as e:  # noqa: BLE001
        return HealResult(
            locator=None, confidence="low",
            reason=f"LLM call failed: {type(e).__name__}: {e}",
            new_fingerprint=None, backend="llm",
        )

    if decision.element_index < 0 or decision.element_index >= len(candidates):
        return HealResult(
            locator=None,
            confidence=decision.confidence,
            reason=f"LLM refused: {decision.reason}",
            new_fingerprint=None,
            backend="llm",
        )

    chosen = candidates[decision.element_index]
    xpath = chosen.get("xpath")
    if not xpath:
        return HealResult(
            locator=None, confidence="low",
            reason="picked candidate has no xpath",
            new_fingerprint=None, backend="llm",
        )
    try:
        loc = page.locator(f"xpath={xpath}").first
    except Exception as e:  # noqa: BLE001
        return HealResult(
            locator=None, confidence="low",
            reason=f"could not build locator from xpath: {e}",
            new_fingerprint=None, backend="llm",
        )

    return HealResult(
        locator=loc,
        confidence=decision.confidence,
        reason=decision.reason,
        new_fingerprint=_candidate_to_fingerprint(chosen),
        backend="llm",
    )


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class LocatorRepair:
    """L3 self-heal entry point. Dispatches deterministic vs LLM."""

    def __init__(
        self,
        client: Any | None = None,
        model: str | None = None,
        *,
        complete_structured: Any | None = None,
        Message: Any | None = None,
    ) -> None:
        """``client`` is an AIClient or None. The complete_structured /
        Message types are injected so this module doesn't depend on
        pilot.agent.* (which would create a circular import — the
        runner is in pilot/, agent code in pilot/agent/)."""
        self.client = client
        self.model = model
        self._complete_structured = complete_structured
        self._Message = Message

    def heal(
        self,
        page: Page,
        original_fp: ElementFingerprint,
        semantic_label: str | None = None,
    ) -> HealResult:
        if (
            self.client is not None
            and self._complete_structured is not None
            and self._Message is not None
        ):
            return _heal_with_llm(
                page,
                original_fp,
                semantic_label,
                client=self.client,
                model=self.model,
                complete_structured=self._complete_structured,
                Message=self._Message,
            )
        return _heal_deterministic(page, original_fp)


def make_repair_for_runner(
    client: Any | None = None, model: str | None = None
) -> "LocatorRepair":
    """Build a LocatorRepair safe to call from the sync skill_runner.

    If ``client`` is None, returns a deterministic-only repair.
    If a client is provided, wraps the async complete_structured helper
    in a sync shim so the runner thread can call it without leaking
    asyncio into its execution path. The shim spins up a fresh event
    loop per call (via asyncio.run); inside an asyncio.to_thread
    worker that's safe and bounded.
    """
    if client is None:
        return LocatorRepair()
    import asyncio
    from pilot.agent.ai_client import Message
    from pilot.agent.ai_client.structured import (
        complete_structured as async_complete_structured,
    )

    def sync_complete_structured(
        client, *,
        messages,
        response_model,
        model=None,
        temperature=0.0,
        max_retries=2,
    ):
        coro = async_complete_structured(
            client,
            messages=messages,
            response_model=response_model,
            model=model,
            temperature=temperature,
            max_retries=max_retries,
        )
        # Runner thread doesn't have a running loop; asyncio.run is fine.
        return asyncio.run(coro)

    return LocatorRepair(
        client=client,
        model=model,
        complete_structured=sync_complete_structured,
        Message=Message,
    )
