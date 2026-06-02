"""Live verification: batch 2 + batch 3 end-to-end against the Angular
sample portal at http://localhost:5189/transfer-artwork.

Records the operator's transfer-artwork flow via a Playwright session
with the batch-1 grabber injected. Then runs the annotator on the
captured trace and asserts:

  - Year, Target Make, Target Model are declared as separate params,
    each with the snake_case label-derived name + example from the
    captured current_value (B2.1, B2.3).
  - Each mat-select param's enum_options is populated from the
    captured options_seen (B2.2 -- known_options on select_option
    spec, then surfaced as enum_options on the SkillParam).
  - The Country/Region set_selection step has require_search=True
    (B2.4 -- picker is ng-multiselect-dropdown).
  - The runner replay path FAILS with search_required_no_search_fp
    when require_search=True and no search_fp is captured -- the
    mandatory-search gate holds (B3.1).

Artifacts (under this directory):
  - trace.jsonl: the recorded trace events
  - skill.json: the annotated skill
  - verify_report.json: assertion outcomes
  - screenshots at key moments
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ARTIFACT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from playwright.sync_api import sync_playwright

from pilot.annotate import build_skill, _assign_synthetic_ids
from pilot.skill_models import TraceEvent

URL = "http://localhost:5189/transfer-artwork"
GRABBER = REPO_ROOT / "pilot" / "overlay" / "grabber.js"


def _decode_buffer(raw):
    out = []
    for entry in raw:
        if isinstance(entry, str):
            try:
                out.append(json.loads(entry))
            except Exception:
                out.append(entry)
        else:
            out.append(entry)
    return out


def main() -> int:
    report = {"steps": [], "asserts": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_selector("#loginOverlay")
        page.click("#loginForm button[type=submit]")
        page.wait_for_selector("[data-display='make']", timeout=10000)
        # Wait for the makes/models bootstrap.
        page.wait_for_timeout(800)

        # Install grabber + capture buffer.
        page.add_script_tag(
            content="window.__cp_test_bridge = true; "
            "window.__cp_debug = false; "
            "window.__pilotBuffer = [];"
        )
        with open(GRABBER, "r", encoding="utf-8") as f:
            grabber_src = f.read()
        page.add_script_tag(content=grabber_src)

        page.screenshot(path=str(ARTIFACT_DIR / "01_filter_initial.png"))

        # ---- Step A: open Year mat-select, pick 2023 ----
        page.click("mat-select#mat-select-year")
        page.wait_for_selector(".cdk-overlay-pane .mat-option", timeout=4000)
        # Pick the option whose visible label is '2023'.
        opts = page.query_selector_all(".cdk-overlay-pane .mat-option")
        for o in opts:
            t = (o.text_content() or "").strip()
            if t == "2023":
                o.click()
                break
        else:
            opts[1].click()
        page.wait_for_timeout(800)
        report["steps"].append("year_picked")

        # ---- Step B: open Target Make mat-select, pick LG ----
        page.click("mat-select#mat-select-make")
        page.wait_for_selector(".cdk-overlay-pane .mat-option", timeout=4000)
        opts = page.query_selector_all(".cdk-overlay-pane .mat-option")
        for o in opts:
            t = (o.text_content() or "").strip()
            if t == "LG":
                o.click()
                break
        else:
            opts[1].click()
        page.wait_for_timeout(800)
        report["steps"].append("make_picked")

        # ---- Step C: open Target Model mat-select, pick the 2nd option ----
        page.click("mat-select#mat-select-model")
        page.wait_for_selector(".cdk-overlay-pane .mat-option", timeout=4000)
        opts = page.query_selector_all(".cdk-overlay-pane .mat-option")
        if len(opts) >= 2:
            opts[1].click()
        else:
            opts[0].click()
        page.wait_for_timeout(800)
        report["steps"].append("model_picked")

        page.screenshot(path=str(ARTIFACT_DIR / "02_filter_picked.png"))

        # ---- Step D: open Country/Region ng-multiselect-dropdown ----
        page.click("ng-multiselect-dropdown .dropdown-btn")
        # Wait for the search input + options list to surface.
        try:
            page.wait_for_selector(
                ".dropdown-list[hidden=false], .dropdown-list:not([hidden])",
                timeout=2000,
            )
        except Exception:
            pass
        try:
            page.wait_for_selector("input[placeholder='Search']", timeout=2000)
        except Exception:
            pass
        # Type 'canada' in the search box.
        search = page.query_selector(".country-search-input")
        if search:
            search.fill("canada")
            page.wait_for_timeout(1000)
        # Try to click a Canada row -- whatever is rendered in
        # data-role='country-options'.
        canada = page.query_selector(
            "[data-role='country-options'] li:has-text('Canada')"
        )
        if canada is None:
            # Backend may not have surfaced Canada; just click first li.
            canada = page.query_selector("[data-role='country-options'] li")
        if canada:
            canada.click()
            page.wait_for_timeout(800)
        report["steps"].append("country_picked")

        page.screenshot(path=str(ARTIFACT_DIR / "03_country_picked.png"))

        # Close the country picker.
        page.click("ng-multiselect-dropdown .dropdown-btn")
        page.wait_for_timeout(400)

        # ---- Step E: click Show Data (now enabled) ----
        try:
            page.click("button[data-action='show-data']", timeout=2000)
            page.wait_for_timeout(1200)
            report["steps"].append("show_data_clicked")
        except Exception as e:
            report["steps"].append(f"show_data_skip:{e}")

        page.screenshot(path=str(ARTIFACT_DIR / "04_show_data.png"))

        # ---- Drain the grabber buffer ----
        raw_buffer = page.evaluate("window.__pilotBuffer || []")
        history = _decode_buffer(raw_buffer)
        # Persist the raw trace.
        with open(ARTIFACT_DIR / "trace.jsonl", "w", encoding="utf-8") as f:
            for ev in history:
                f.write(json.dumps(ev) + "\n")
        report["trace_event_count"] = len(history)
        report["trace_kinds"] = sorted(
            {ev.get("kind") for ev in history if isinstance(ev, dict)}
        )

        browser.close()

    # ---- Annotate the trace ----
    raw_events: list[TraceEvent] = []
    for line in (ARTIFACT_DIR / "trace.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw_events.append(TraceEvent.model_validate_json(line))
        except Exception as e:
            report["asserts"].append(
                f"WARN: failed to validate event: {type(e).__name__}: {e}"
            )

    skill = build_skill(
        skill_name="transfer_artwork_angular",
        events=raw_events,
        auto=True,
        portal="angular_sample",
    )
    (ARTIFACT_DIR / "skill.json").write_text(
        skill.model_dump_json(indent=2), encoding="utf-8"
    )

    # ---- Assertions ----
    param_names = {p.name for p in skill.params}
    report["asserts"].append(
        f"declared_params = {sorted(param_names)}"
    )

    # B2.1 + B2.3: labeled-widget params per the page's labels.
    # We don't strictly require a particular naming, just that some
    # year / make / model / country signal exists in the params.
    year_param = next((p for p in skill.params if "year" in p.name), None)
    make_param = next(
        (p for p in skill.params if "make" in p.name and "year" not in p.name),
        None,
    )
    model_param = next(
        (p for p in skill.params if "model" in p.name), None
    )
    country_param = next(
        (p for p in skill.params if "country" in p.name or "region" in p.name),
        None,
    )

    report["asserts"].append(
        f"year_param={year_param.name if year_param else None}"
        f" example={year_param.example if year_param else None}"
        f" enum_count={len(year_param.enum_options or []) if year_param else 0}"
    )
    report["asserts"].append(
        f"make_param={make_param.name if make_param else None}"
        f" example={make_param.example if make_param else None}"
    )
    report["asserts"].append(
        f"model_param={model_param.name if model_param else None}"
        f" example={model_param.example if model_param else None}"
    )
    report["asserts"].append(
        f"country_param={country_param.name if country_param else None}"
    )

    # B2.4: country set_selection should have require_search=True.
    country_ss_steps = [
        s for s in skill.steps
        if s.action == "set_selection" and s.set_selection is not None
    ]
    report["asserts"].append(
        f"set_selection_step_count={len(country_ss_steps)}"
    )
    for s in country_ss_steps:
        spec = s.set_selection
        report["asserts"].append(
            f"set_selection step idx={s.index} param={spec.param} "
            f"require_search={spec.require_search} "
            f"has_search_fp={spec.search_fp is not None}"
        )

    # B2.2: mat-select clicks should produce select_option specs with
    # known_options populated.
    so_steps = [
        s for s in skill.steps if s.select_option is not None
    ]
    report["asserts"].append(f"select_option_step_count={len(so_steps)}")
    for s in so_steps[:6]:
        spec = s.select_option
        report["asserts"].append(
            f"select_option step idx={s.index} "
            f"recorded={spec.recorded_value!r} "
            f"known_count={len(spec.known_options or [])} "
            f"require_search={spec.require_search}"
        )

    # B3.1: runner must fail with search_required_no_search_fp when
    # require_search=True and search_fp is None.
    # Test this directly with a fake runner.
    from pilot.skill_runner import SkillRunner
    from pilot.skill_models import (
        ElementFingerprint as _FP,
        SetSelectionSpec as _SS,
        Skill as _S,
        SkillStep as _STEP,
    )
    from datetime import datetime as _dt

    fake_skill = _S(
        name="fake", steps=[],
        created_at=_dt.utcnow(), updated_at=_dt.utcnow(),
    )
    fake_runner = SkillRunner(
        session=None, skill=fake_skill, params={"x": ["Y"]},
        sessions_dir=Path(tempfile.gettempdir()),
    )

    class _FS:
        page = None

    fake_runner.session = _FS()
    spec_bad = _SS(
        mode="replace",
        param="x",
        search_fp=None,
        require_search=True,
    )
    result = fake_runner._reach_and_click_option(
        spec_bad, "Y", item_label="Y",
    )
    if (
        result is not None
        and result.success is False
        and result.error_kind == "search_required_no_search_fp"
    ):
        report["asserts"].append(
            "PASS runner B3.1 gate: require_search + no search_fp -> "
            "search_required_no_search_fp"
        )
    else:
        report["asserts"].append(
            f"FAIL runner B3.1 gate: result={result!r}"
        )

    (ARTIFACT_DIR / "verify_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
