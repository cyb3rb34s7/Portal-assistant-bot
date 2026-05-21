"""Tests for WI-24: safe selector construction.

Today's ``_css_escape`` only handles ``\\`` and ``'``. IDs with ``]``,
double quotes, spaces, colons, non-ASCII break. The fix:
  - Replace runner-side ``_css_escape`` with Playwright's semantic
    locator APIs (``get_by_test_id`` / ``get_by_label``) where possible.
  - Where raw CSS is unavoidable, use ``[attr="value"]`` with proper
    backslash-and-double-quote escaping via the runner's
    ``_attr_locator`` helper (and the symmetric ``_replay_escape_attr``
    in skill_models for the down-converted Replay JSON).
  - Grabber: keep using ``window.CSS.escape``; the readiness +
    css_path selector builders that concatenated test_id raw now
    double-quote and backslash-escape.

Acceptance check (from the plan):
  IDs / names containing ``]`` / quotes / spaces / colons / non-ASCII
  resolve correctly. Tested with each problem character.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

import pytest

from pilot.skill_models import (
    ElementFingerprint,
    Skill,
    _build_replay_selectors,
    _replay_escape_attr,
)
from pilot.skill_runner import SkillRunner


def _runner() -> SkillRunner:
    skill = Skill(
        name="t",
        steps=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    return SkillRunner(
        session=None,  # type: ignore[arg-type]
        skill=skill,
        params={},
        sessions_dir=Path(tempfile.gettempdir()),
    )


# ---------------------------------------------------------------------
# _attr_locator: runner-side safe selector construction
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "row-A-9001",         # baseline
        "row]bracket",        # `]` would close attribute selector
        "row'quote",          # single quote
        'row"doublequote',    # double quote -- escape must apply
        "row with space",
        "row:colon",
        "row.dot",
        "row非ASCII",          # non-ASCII
        "row\\backslash",     # backslash
    ],
)
def test_attr_locator_handles_problem_chars(value: str) -> None:
    """The runner's _attr_locator must emit a parseable
    ``[attr="value"]`` form for every character class that the legacy
    _css_escape missed."""
    runner = _runner()
    sel = runner._attr_locator("data-testid", value)
    assert sel.startswith('[data-testid="')
    assert sel.endswith('"]')
    # Backslash and double-quote inside the value must be escaped.
    inner = sel[len('[data-testid="'):-2]
    raw = inner.replace('\\"', '"').replace("\\\\", "\\")
    assert raw == value


def test_attr_locator_double_quote_escapes_to_backslash_quote() -> None:
    """Specifically: a value containing a double quote becomes
    backslash-quoted (\\\"), not unquoted. Pre-WI-24 the legacy
    interpolation ``[attr='{value}']`` would have left the embedded
    quote alone, producing an unparseable selector when the value
    contained an attribute-closing quote of the OTHER kind."""
    runner = _runner()
    assert runner._attr_locator("id", 'a"b') == '[id="a\\"b"]'


def test_attr_locator_backslash_doubles() -> None:
    """A literal backslash in the value must be emitted as two
    backslashes so the resulting CSS literal parses."""
    runner = _runner()
    assert runner._attr_locator("id", r"row\back") == '[id="row\\\\back"]'


# ---------------------------------------------------------------------
# _replay_escape_attr: down-converted Replay JSON
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "row-A-9001",
        "row]bracket",
        "row'quote",
        'row"doublequote',
        "row with space",
        "row:colon",
        "row非ASCII",
    ],
)
def test_replay_selector_escapes_problem_chars(value: str) -> None:
    """The down-converted Replay JSON's selectors must escape problem
    characters so Puppeteer Replay's selector resolver can parse them.
    Pre-WI-24, `[data-testid='{value}']` interpolation broke."""
    fp = ElementFingerprint(test_id=value, element_id=value)
    selectors = _build_replay_selectors(fp)
    testid_sels = [
        s[0] for s in selectors if s and s[0].startswith("[data-testid=")
    ]
    assert testid_sels, "no testid selector emitted"
    sel = testid_sels[0]
    assert sel.startswith('[data-testid="')
    assert sel.endswith('"]')
    inner = sel[len('[data-testid="'):-2]
    raw = inner.replace('\\"', '"').replace("\\\\", "\\")
    assert raw == value


def test_replay_selector_uses_attr_form_for_id() -> None:
    """Pre-WI-24 the element_id selector emitted ``#{id}`` which broke
    for IDs containing CSS-syntax characters (``:`` / ``.`` / ``[``).
    The fix: emit ``[id="..."]`` with proper escaping."""
    fp = ElementFingerprint(element_id="id:with:colons")
    selectors = _build_replay_selectors(fp)
    id_sels = [s[0] for s in selectors if s and s[0].startswith("[id=")]
    assert id_sels
    assert id_sels[0] == '[id="id:with:colons"]'


def test_replay_escape_attr_helper_idempotent_on_clean_value() -> None:
    """The escape helper is a no-op for values without ``\\`` or
    ``"``; the resulting selector for a clean value is byte-identical
    to the input value."""
    assert _replay_escape_attr("row-A-9001") == "row-A-9001"


def test_legacy_css_escape_kept_for_back_compat() -> None:
    """The legacy ``_css_escape`` helper is preserved as a deprecated
    shim. New code should use ``_attr_locator`` / semantic locators
    instead. The legacy helper handles backslash and single quote
    only; this test pins that behavior so any external caller who
    still imports it knows what they get."""
    from pilot.skill_runner import _css_escape

    assert _css_escape("a") == "a"
    assert _css_escape("a'b") == "a\\'b"
    assert _css_escape("a\\b") == "a\\\\b"
    # Pre-WI-24 deficiency: the helper does NOT escape ``]`` or quotes
    # other than single-quote. That's why _attr_locator exists.
    assert _css_escape("a]b") == "a]b"
