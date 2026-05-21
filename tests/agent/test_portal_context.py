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
