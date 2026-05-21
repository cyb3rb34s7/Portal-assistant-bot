"""PortalContext regression coverage."""

from __future__ import annotations

from pathlib import Path

import yaml

from pilot.agent.schemas.portal_context import PortalContext


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
