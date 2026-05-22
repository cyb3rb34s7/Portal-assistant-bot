"""PortalContext regression coverage."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pilot.agent.schemas.portal_context import (
    IdempotencyCapability,
    PortalContext,
    WaitPolicy,
)


def test_sample_portal_replay_idempotency_overrides_app_key() -> None:
    """The sample portal must let the runner's stable retry key win."""
    repo_root = Path(__file__).resolve().parents[2]
    raw = yaml.safe_load(
        (repo_root / "portals" / "sample_portal" / "context.yaml").read_text(
            encoding="utf-8"
        )
    )

    ctx = PortalContext.model_validate(raw)

    assert ctx.idempotency.enabled is True
    assert ctx.idempotency.allow_existing_header is False


def test_idempotency_enabled_requires_explicit_scope() -> None:
    """F-08d: enabling idempotency with no endpoint_patterns and
    scope_all_endpoints=False must be rejected. The old WI-04 default
    of 'inject on every non-GET endpoint' is silent and dangerous."""
    with pytest.raises(ValueError, match="scope_all_endpoints"):
        IdempotencyCapability(enabled=True, endpoint_patterns=[])


def test_idempotency_enabled_with_explicit_scope_all_passes() -> None:
    """Operators who genuinely want all-endpoint scoping must say so."""
    cap = IdempotencyCapability(
        enabled=True,
        endpoint_patterns=[],
        scope_all_endpoints=True,
    )
    assert cap.enabled is True
    assert cap.scope_all_endpoints is True


def test_idempotency_enabled_with_endpoint_patterns_passes() -> None:
    """Scoped endpoint_patterns is the recommended path; no scope_all flag needed."""
    cap = IdempotencyCapability(
        enabled=True,
        endpoint_patterns=["/api/assets/", "/api/orders/"],
    )
    assert cap.scope_all_endpoints is False
    assert cap.endpoint_patterns == ["/api/assets/", "/api/orders/"]


def test_idempotency_disabled_skips_scope_validation() -> None:
    """Disabled is the safe default and doesn't need either knob."""
    cap = IdempotencyCapability()
    assert cap.enabled is False


def test_wait_policy_defaults_live_on_portal_context() -> None:
    """F-08c: timeout defaults live on PortalContext.wait_policy so
    annotators can read them per-portal instead of inheriting hardcoded
    class-level defaults from the StepEffect / Expectation models."""
    ctx = PortalContext(portal_id="p", name="P", base_url="http://x")
    assert isinstance(ctx.wait_policy, WaitPolicy)
    assert ctx.wait_policy.network_max_ms == 5000
    assert ctx.wait_policy.dom_timeout_ms == 5000
    assert ctx.wait_policy.dom_stable_ms == 250
    assert ctx.wait_policy.navigation_timeout_ms == 5000
    assert ctx.wait_policy.assertion_timeout_ms == 4000
    assert ctx.wait_policy.options_snapshot_max == 500


def test_wait_policy_followup3_grabber_heuristic_fields_defaults() -> None:
    """Followup #3: grabber.js heuristics surfaced through WaitPolicy.

    These five fields replace literal constants in grabber.js (the
    runner pushes each one onto window.__cp_* before the grabber's
    next read; the grabber keeps the literal as last-resort fallback)."""
    pol = WaitPolicy()
    assert pol.input_debounce_ms == 400
    assert pol.dom_mutation_burst_ms == 200
    assert pol.attribution_fallback_window_ms == 50
    assert pol.hover_submenu_search_cap == 200
    assert pol.page_snapshot_option_cap == 40


def test_wait_policy_followup3_roundtrip_with_overrides() -> None:
    """Operators override the followup #3 fields in YAML; verify the
    overrides survive a model_dump_json -> model_validate_json roundtrip
    (the load path PortalContext goes through at portal_context_loader
    boot)."""
    pol = WaitPolicy(
        input_debounce_ms=250,
        dom_mutation_burst_ms=350,
        attribution_fallback_window_ms=0,  # zero = "no fallback"
        hover_submenu_search_cap=500,
        page_snapshot_option_cap=80,
    )
    restored = WaitPolicy.model_validate_json(pol.model_dump_json())
    assert restored.input_debounce_ms == 250
    assert restored.dom_mutation_burst_ms == 350
    assert restored.attribution_fallback_window_ms == 0
    assert restored.hover_submenu_search_cap == 500
    assert restored.page_snapshot_option_cap == 80


def test_runner_ensure_watchers_pushes_grabber_globals() -> None:
    """Followup #3: SkillRunner._ensure_watchers must push each new
    wait_policy field to window.__cp_* before the grabber reads it.

    We can't drive Playwright in unit tests, but we can verify the
    Python side calls page.evaluate with the expected payload by
    substituting a stub page object."""
    from datetime import datetime
    from pilot.skill_models import Skill
    from pilot.skill_runner import SkillRunner
    from pilot.agent.schemas.portal_context import WaitPolicy

    class _StubPage:
        def __init__(self):
            self.evaluate_calls: list[tuple[str, object]] = []

        def evaluate(self, script: str, arg=None):
            self.evaluate_calls.append((script, arg))
            return None

    skill = Skill(
        name="t",
        steps=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    runner = SkillRunner(
        session=None,  # type: ignore[arg-type]
        skill=skill,
        params={},
        sessions_dir=Path("."),
    )
    runner.wait_policy = WaitPolicy(
        input_debounce_ms=250,
        dom_mutation_burst_ms=300,
        attribution_fallback_window_ms=25,
        hover_submenu_search_cap=400,
        page_snapshot_option_cap=80,
        options_snapshot_max=600,
        request_log_cap=150,
    )
    page = _StubPage()
    runner._ensure_watchers(page)  # type: ignore[arg-type]

    # The first evaluate call should be the globals-push with our
    # custom values; the second is the watcher install JS.
    assert len(page.evaluate_calls) >= 1
    _script, payload = page.evaluate_calls[0]
    assert payload == {
        "cap": 150,
        "opts_cap": 600,
        "input_debounce_ms": 250,
        "dom_mutation_burst_ms": 300,
        "attribution_fallback_ms": 25,
        "hover_submenu_cap": 400,
        "page_snapshot_opt_cap": 80,
    }
    # Sanity: the script body actually references every global we
    # expect the grabber to read.
    assert "__cp_input_debounce_ms" in _script
    assert "__cp_dom_mutation_burst_ms" in _script
    assert "__cp_attribution_fallback_ms" in _script
    assert "__cp_hover_submenu_search_cap" in _script
    assert "__cp_page_snapshot_option_cap" in _script
    assert "__cp_opts_cap" in _script
    assert "__cp_request_log_cap" in _script
