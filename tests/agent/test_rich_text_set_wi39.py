"""WI-39: rich text editor / contenteditable workflows.

Plan acceptance check:
  Typing "Hello **bold**" into a contenteditable replays the same
  content. The grabber's WI-39 burst capture collapses a typing burst
  inside a contenteditable into ONE ``rich_text_input`` event; the
  annotator collapses that to a single ``rich_text_set`` step with a
  RichTextSpec; the runner is no longer in _UNIMPLEMENTED_ACTIONS.

Tests cover the deterministic pipeline (no live browser):
  - Schema: RichTextSpec validates, persists across model_dump/validate.
  - Schema: rich_text field on SkillStep accepts the spec.
  - Schema: TraceEvent.rich_text_html / rich_text_framework optional.
  - Detector: a rich_text_input event with control_kind=contenteditable
    is claimed by _detect_rich_text_clusters as one rich_text_set
    cluster, NOT folded into fill_submit.
  - Annotator: build_skill emits one ``rich_text_set`` step + a
    ``string`` SkillParam whose example is the recorded textContent.
  - Annotator: format inference -- html vs plain based on tag presence
    in the recorded innerHTML.
  - Runner: ``rich_text_set`` is no longer in _UNIMPLEMENTED_ACTIONS
    (the runner dispatches it through _do_rich_text_set).
"""

from __future__ import annotations

from datetime import datetime

from pilot.annotate import (
    _build_rich_text_spec,
    _detect_rich_text_clusters,
    build_causality_graph,
    build_skill,
)
from pilot.skill_models import (
    ElementFingerprint,
    RichTextSpec,
    Skill,
    SkillStep,
    TraceEvent,
)
from pilot.skill_runner import _UNIMPLEMENTED_ACTIONS


def _rich_text_event(
    event_id: str,
    value: str,
    sequence: int,
    *,
    html: str | None = None,
    framework: str = "unknown",
    test_id: str = "input-description",
) -> TraceEvent:
    return TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(
            test_id=test_id,
            tag="div",
            control_kind="contenteditable",
            value_kind="html",
            contenteditable=True,
        ),
        value=value,
        rich_text_html=html if html is not None else value,
        rich_text_framework=framework,  # type: ignore[arg-type]
        page_url="http://x",
        event_id=event_id,
        interaction_id=event_id,
        caused_by=None,
        sequence=sequence,
        source="user_input",
        raw_event_kind="rich_text_input",
    )


# ----- schema --------------------------------------------------------------


def test_rich_text_spec_roundtrip() -> None:
    spec = RichTextSpec(
        value_param="description",
        format="html",
        embed_policy="strip",
        paste_strategy="input_event",
        framework_hint="quill",
        recorded_html="<p>Hello <strong>bold</strong></p>",
        recorded_text="Hello bold",
    )
    payload = spec.model_dump()
    restored = RichTextSpec(**payload)
    assert restored.value_param == "description"
    assert restored.format == "html"
    assert restored.embed_policy == "strip"
    assert restored.paste_strategy == "input_event"
    assert restored.framework_hint == "quill"


def test_skill_step_carries_rich_text_spec() -> None:
    step = SkillStep(
        index=0,
        action="rich_text_set",
        fingerprint=ElementFingerprint(test_id="input-description"),
        rich_text=RichTextSpec(value_param="desc", format="plain"),
    )
    payload = step.model_dump()
    restored = SkillStep(**payload)
    assert restored.rich_text is not None
    assert restored.rich_text.value_param == "desc"
    assert restored.rich_text.format == "plain"


def test_trace_event_rich_text_fields_optional() -> None:
    # Non-rich-text traces still parse: fields default None.
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        page_url="http://x",
    )
    assert ev.rich_text_html is None
    assert ev.rich_text_framework is None


# ----- detector ------------------------------------------------------------


def test_detector_claims_rich_text_input_event() -> None:
    ev = _rich_text_event("e1", "Hello world", 1)
    causality = build_causality_graph([ev])
    consumed: set[str] = set()
    clusters = _detect_rich_text_clusters([ev], causality, consumed)
    assert len(clusters) == 1
    assert clusters[0].cluster_kind == "rich_text_set"
    assert clusters[0].raw_event_ids == ["e1"]
    assert "e1" in consumed


def test_detector_skips_non_rich_text_input() -> None:
    # raw_event_kind != "rich_text_input" -> not claimed.
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(test_id="title", control_kind="text_input"),
        value="hello",
        page_url="http://x",
        event_id="plain1",
        interaction_id="plain1",
        sequence=1,
        raw_event_kind="input",
    )
    causality = build_causality_graph([ev])
    consumed: set[str] = set()
    clusters = _detect_rich_text_clusters([ev], causality, consumed)
    assert clusters == []
    assert "plain1" not in consumed


def test_detector_requires_contenteditable_or_html_evidence() -> None:
    # raw_event_kind says rich_text_input but no contenteditable evidence:
    # detector skips rather than mis-cluster.
    ev = TraceEvent(
        ts=datetime.utcnow(),
        kind="input_change",
        fingerprint=ElementFingerprint(test_id="x", control_kind="text_input"),
        value="hi",
        rich_text_html=None,
        page_url="http://x",
        event_id="weird1",
        interaction_id="weird1",
        sequence=1,
        raw_event_kind="rich_text_input",
    )
    causality = build_causality_graph([ev])
    consumed: set[str] = set()
    clusters = _detect_rich_text_clusters([ev], causality, consumed)
    assert clusters == []


# ----- spec build ----------------------------------------------------------


def test_build_spec_infers_plain_format_for_plaintext() -> None:
    ev = _rich_text_event(
        "e1", "Hello world", 1, html="<div>Hello world</div>"
    )
    spec = _build_rich_text_spec(ev, "description")
    assert spec.format == "plain"
    assert spec.embed_policy == "preserve"
    assert spec.value_param == "description"


def test_build_spec_infers_html_format_with_markup() -> None:
    ev = _rich_text_event(
        "e1",
        "Hello bold",
        1,
        html="<p>Hello <strong>bold</strong></p>",
    )
    spec = _build_rich_text_spec(ev, "description")
    assert spec.format == "html"
    assert spec.embed_policy == "strip"


def test_build_spec_carries_framework_hint() -> None:
    ev = _rich_text_event(
        "e1", "Hello", 1, html="<p>Hello</p>", framework="quill"
    )
    spec = _build_rich_text_spec(ev, "description")
    assert spec.framework_hint == "quill"


# ----- annotator end-to-end -----------------------------------------------


def test_annotator_emits_rich_text_set_step_and_param() -> None:
    ev = _rich_text_event(
        "e1",
        "Hello bold",
        1,
        html="<p>Hello <strong>bold</strong></p>",
    )
    skill = build_skill(
        skill_name="rich_text_demo",
        events=[ev],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    # Exactly one rich_text_set step.
    rich_text_steps = [s for s in skill.steps if s.action == "rich_text_set"]
    assert len(rich_text_steps) == 1, (
        f"expected one rich_text_set, got {[s.action for s in skill.steps]}"
    )
    rt = rich_text_steps[0].rich_text
    assert rt is not None
    assert rt.format == "html"
    # A SkillParam was declared for the editor.
    param_names = [p.name for p in skill.params]
    assert rt.value_param in param_names


def test_annotator_does_not_fold_rich_text_into_fill_submit() -> None:
    # A rich_text burst followed by a click on Save should NOT be
    # collapsed by fill_submit (which would lose the framework /
    # paste strategy metadata). Verify the cluster ordering preserves
    # rich_text_set + click as two separate steps.
    rt_ev = _rich_text_event(
        "rt1",
        "Hello bold",
        1,
        html="<p>Hello <strong>bold</strong></p>",
    )
    save_click = TraceEvent(
        ts=datetime.utcnow(),
        kind="click",
        fingerprint=ElementFingerprint(
            test_id="btn-save",
            tag="button",
            text="Save",
            control_kind="button",
        ),
        page_url="http://x",
        event_id="save1",
        interaction_id="save1",
        sequence=2,
        source="user_click",
        raw_event_kind="click",
    )
    skill = build_skill(
        skill_name="rich_text_then_save",
        events=[rt_ev, save_click],
        base_url="http://x",
        portal="sample_portal",
        auto=True,
    )
    actions = [s.action for s in skill.steps]
    assert "rich_text_set" in actions
    # Save click must remain a separate step (click, not fill_submit).
    assert "click" in actions


# ----- runner stub --------------------------------------------------------


def test_runner_rich_text_set_no_longer_unimplemented() -> None:
    assert "rich_text_set" not in _UNIMPLEMENTED_ACTIONS
