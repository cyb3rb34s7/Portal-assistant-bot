"""2026-06-02: regression test for the real-portal label-capture
diagnosis (DOCS/reviews/2026-06-02_real-portal-label-capture-diagnosis.md).

Loads tmp/portaldata/page-filled.html into a jsdom Window through the
Node bridge at tools/run_grabber_helpers.js, runs the IIFE-private
grabber helpers against the EXACT widget trees the real Frame TV portal
produced, and asserts that the three bugs the diagnosis identified are
fixed:

  B1. _resolveSemanticTarget lifts a click on the inner
      .mat-select-trigger div up to the <mat-select> widget root.
  B2. getAccessibleName resolves "Target Model" / "Source Year" /
      "Country/Region" via the preceding-sibling / inside-container
      <label> patterns.
  B3. _currentDisplayValue exposes the mat-select's
      .mat-select-value-text ("18_KANTM2_8K"), the mat-checkbox's
      aria-checked boolean, and the ng-multiselect-dropdown's chip text.
  B4. _collectMatSelectOptions reads mat-option rows from an open
      cdk-overlay-pane (synthetic in this static-HTML test).
  B5. _isSearchLikeInput detects placeholder="Search" + multiselect
      search inputs so the longer debounce window applies to them.

The test skips when Node or the jsdom dep tree isn't available so the
CI can keep running on environments that don't have npm installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE_FILLED = REPO_ROOT / "tmp" / "portaldata" / "page-filled.html"
BRIDGE = REPO_ROOT / "tools" / "run_grabber_helpers.js"
JSDOM = REPO_ROOT / "tmp" / "jsdom-deps" / "node_modules" / "jsdom"


def _have_bridge() -> bool:
    """Bridge is runnable only when Node + jsdom + the fixture are all on disk."""
    return (
        shutil.which("node") is not None
        and PAGE_FILLED.is_file()
        and BRIDGE.is_file()
        and JSDOM.is_dir()
    )


@pytest.fixture(scope="module")
def probe_result() -> dict:
    if not _have_bridge():
        pytest.skip(
            "node + jsdom + page-filled.html fixture required for "
            "grabber-helper regression tests"
        )
    env = os.environ.copy()
    res = subprocess.run(
        ["node", str(BRIDGE), str(PAGE_FILLED)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=30,
    )
    assert res.returncode == 0, (
        f"bridge failed: stderr={res.stderr!r} stdout={res.stdout!r}"
    )
    out = res.stdout.strip()
    assert out, "bridge produced no stdout"
    # Bridge may print debug lines; take the LAST line (the JSON payload).
    payload = out.splitlines()[-1]
    return json.loads(payload)


# ---- B1: semantic widget root resolution ---------------------------------


def test_b1_resolves_mat_select_from_inner_trigger_div(probe_result: dict) -> None:
    """A click on the inner div inside .mat-select-trigger must resolve
    to the outer <mat-select> widget root. This is the fix for trace.jsonl
    event #4 (Target Model) coming out as tag=div / role=null."""
    assert probe_result["has_mat_select_1"] is True
    assert probe_result["inner_click_tag"] == "div"
    assert probe_result["resolved_tag"] == "mat-select"
    assert probe_result["resolved_id"] == "mat-select-1"


def test_b1_fingerprint_records_mat_select_role(probe_result: dict) -> None:
    """The fingerprint on the resolved root reports role=listbox (the
    mat-select's explicit role) rather than the inner div's null role."""
    assert probe_result["target_model_fp_tag"] == "mat-select"
    assert probe_result["target_model_fp_role"] == "listbox"


# ---- B2: extended label discovery ----------------------------------------


def test_b2_target_model_label_discovered(probe_result: dict) -> None:
    """getAccessibleName on <mat-select#mat-select-1> must return
    "Target Model" (from the preceding-sibling <label>). Pre-fix this
    returned None because the WAI-ARIA cascade stops at label-ancestor."""
    name = probe_result["target_model_acc_name"]
    assert name.startswith("Target Model"), (
        f"expected acc name to start with 'Target Model'; got {name!r}"
    )


def test_b2_source_year_label_discovered(probe_result: dict) -> None:
    """Same sibling-label pattern applies to the Year picker."""
    name = probe_result["source_year_acc_name"]
    assert name.startswith("Source Year"), (
        f"expected 'Source Year...' acc name; got {name!r}"
    )


def test_b2_country_region_label_discovered(probe_result: dict) -> None:
    """The ng-multiselect-dropdown for Country/Region uses the
    inside-container <label> shape (Shape A): the wrapper div has
    both the <label> and a child div containing the dropdown. The
    extended discovery resolves the inside-container label, NOT a
    preceding-sibling label from a different filter wrapper."""
    name = probe_result["country_acc_name"]
    assert name.startswith("Country/Region"), (
        f"expected 'Country/Region...' acc name; got {name!r}"
    )


def test_b2_filter_label_does_not_leak_into_country(probe_result: dict) -> None:
    """Regression guard for the 'wrapper preceding-sibling search picks
    up an unrelated previous wrapper's label' bug. The Filter
    ng-multiselect-dropdown must report Filter:, not the previous
    Target Model label."""
    assert probe_result["filter_acc_name"].startswith("Filter")


# ---- B3: current_value capture --------------------------------------------


def test_b3_target_model_current_value(probe_result: dict) -> None:
    """The mat-select's .mat-select-value-text content -- the strongest
    disambiguator two unlabeled dropdowns can carry -- must surface as
    current_value on the fingerprint."""
    assert probe_result["target_model_current_value"] == "18_KANTM2_8K"
    assert probe_result["target_model_fp_current_value"] == "18_KANTM2_8K"


def test_b3_source_year_current_value(probe_result: dict) -> None:
    """Year picker exposes "2018" via mat-select-value-text."""
    assert probe_result["source_year_current_value"] == "2018"


def test_b3_country_region_current_value(probe_result: dict) -> None:
    """The ng-multiselect-dropdown's chip text (Albania) surfaces as
    current_value; chip's trailing 'x' close glyph is trimmed."""
    assert probe_result["country_current_value"] == "Albania"


def test_b3_mat_checkbox_current_value(probe_result: dict) -> None:
    """mat-checkbox exposes its aria-checked boolean string for the
    planner / annotator."""
    # mat-checkbox-2 has aria-checked="true" in the source HTML.
    assert probe_result["mat_checkbox_2_current_value"] == "true"


# ---- B4: mat-select panel option capture ----------------------------------


def test_b4_collect_mat_select_options(probe_result: dict) -> None:
    """When the panel is open (synthetic in this test, but matches the
    cdk-overlay-pane shape Material renders), _collectMatSelectOptions
    returns {value, label} per mat-option."""
    opts = probe_result["collected_options"]
    assert isinstance(opts, list)
    values = {o["value"]: o["label"] for o in opts}
    assert values == {
        "mat-option-fake-1": "2024",
        "mat-option-fake-2": "2023",
    }


# ---- B5: search-input detection (drives the longer debounce window) ------


def test_b5_search_input_detected_by_placeholder(probe_result: dict) -> None:
    """An <input placeholder="Search"> is search-like so it gets the
    longer (600 ms default) debounce window."""
    assert probe_result["has_search_input"] is True
    assert probe_result["search_input_is_search_like"] is True


def test_b5_multiselect_search_input_detected(probe_result: dict) -> None:
    """The ng-multiselect-dropdown's filter <input> (also
    placeholder="Search", also aria-label="multiselect-search") is
    detected as search-like."""
    assert probe_result["has_country_search_input"] is True
    assert probe_result["country_search_is_search_like"] is True


# ---- Producer + consumer roundtrip: current_value on the fingerprint -----


def test_producer_consumer_current_value_roundtrip() -> None:
    """Anti-drift guard: the grabber emits current_value on the
    fingerprint payload; ElementFingerprint must round-trip it.

    Validates that a synthetic fingerprint dict with current_value
    populates the field after pydantic validation (the previous shape
    silently dropped unknown fields in pre-2026-06-02 code paths)."""
    from pilot.skill_models import ElementFingerprint

    fp = ElementFingerprint.model_validate(
        {
            "tag": "mat-select",
            "role": "listbox",
            "accessible_name": "Target Model",
            "current_value": "18_KANTM2_8K",
        }
    )
    assert fp.current_value == "18_KANTM2_8K"
    assert fp.accessible_name == "Target Model"
    # Re-serialize and confirm the field survives JSON roundtrip.
    again = ElementFingerprint.model_validate_json(fp.model_dump_json())
    assert again.current_value == "18_KANTM2_8K"
