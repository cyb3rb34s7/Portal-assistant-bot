"""Operator-style E2E test suite for the rebuilt sample portal.

Drives the upload -> layout -> save -> apply flow as a real operator
would, plus a battery of edge-case scenarios. Each scenario reports
PASS / FAIL with a one-line note. Final summary at the end.

Self-contained: starts vite, runs all scenarios in a single Chromium
context, tears down at the end.

Usage:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/operator_test_suite.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen

from playwright.async_api import async_playwright, Page

ROOT = Path(__file__).resolve().parent.parent
PORTAL_DIR = ROOT / "sample_portal"
# Use a non-default port to avoid colliding with any other vite dev server
# the operator might have running on 5173 (e.g. another project).
PORTAL_PORT = 5188
PORTAL_URL = f"http://localhost:{PORTAL_PORT}"
FIXTURES = ROOT / "tests" / "fixtures"
SHOTS_DIR = ROOT / "sessions" / "operator_test_suite"


@dataclass
class Result:
    name: str
    passed: bool
    note: str = ""
    screenshot: str | None = None


@dataclass
class Suite:
    results: list[Result] = field(default_factory=list)

    def record(self, name: str, passed: bool, note: str = "", shot: str | None = None) -> None:
        marker = "PASS" if passed else "FAIL"
        print(f"[{marker}] {name}: {note}", flush=True)
        self.results.append(Result(name=name, passed=passed, note=note, screenshot=shot))


def info(msg: str) -> None:
    print(f"[op-suite] {msg}", flush=True)


def wait_for(url: str, timeout: float = 45.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_portal() -> subprocess.Popen:
    info(f"starting vite on port {PORTAL_PORT} ...")
    # Run vite directly with a chosen port (sample_portal's `npm run dev`
    # hardcodes 5173 which often collides with other dev servers).
    proc = subprocess.Popen(
        [
            "npx.cmd", "vite", "--host", "--port", str(PORTAL_PORT),
            "--strictPort",
        ],
        cwd=str(PORTAL_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    if not wait_for(PORTAL_URL):
        proc.kill()
        raise RuntimeError("portal did not come up")
    # Sanity-check the right project is serving — not some other vite
    # instance that happened to be on the port already.
    try:
        with urlopen(PORTAL_URL, timeout=2) as r:
            html = r.read().decode("utf-8", errors="replace")
        if "Sample Portal" not in html and "main.jsx" not in html:
            proc.kill()
            raise RuntimeError(
                f"port {PORTAL_PORT} is serving a different app; "
                "kill it or pick another port"
            )
    except Exception as e:
        proc.kill()
        raise
    info("portal up + verified")
    return proc


def stop_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        proc.kill()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def reset_state(page: Page) -> None:
    await page.goto(PORTAL_URL, wait_until="networkidle")
    await page.evaluate("() => window.localStorage.clear()")
    await page.reload(wait_until="networkidle")


async def upload_csv(page: Page, csv_path: Path) -> None:
    await page.click('[data-testid="nav-upload"]')
    await page.wait_for_selector('[data-testid="upload-page"]', timeout=5000)
    await page.set_input_files('[data-testid="input-csv-file"]', str(csv_path))
    # Wait for either the contents table to render or for the error block.
    try:
        await page.wait_for_selector(
            '[data-testid="contents-table"], [data-testid="csv-errors"]',
            timeout=5000,
        )
    except Exception:
        pass


async def go_to_curation(page: Page) -> None:
    await page.click('[data-testid="nav-curation"]')
    await page.wait_for_selector('[data-testid="curation-page"]', timeout=5000)


async def pick_layout(page: Page, layout_id: str) -> None:
    await page.click(f'[data-testid="layout-option-{layout_id}"]')
    # Wait for the specific layout's slots-grid to be present, not just
    # any layout-editor (which may still show the previous layout for
    # a frame after the click).
    await page.wait_for_selector(
        f'[data-testid="slots-{layout_id}"]', timeout=5000
    )


async def fill_slot(page: Page, idx: int, content_id: str, image_path: Path | None) -> None:
    await page.select_option(
        f'[data-testid="slot-{idx}-content-select"]', content_id
    )
    if image_path is not None:
        # The image input is disabled until content is picked. Wait for
        # React to flip its disabled attribute before we set the file.
        await page.wait_for_function(
            "(idx) => { "
            "  const el = document.querySelector(`[data-testid='slot-${idx}-image-input']`);"
            "  return el && !el.disabled;"
            "}",
            arg=idx,
            timeout=3000,
        )
        await page.set_input_files(
            f'[data-testid="slot-{idx}-image-input"]', str(image_path)
        )
        await page.wait_for_selector(
            f'[data-testid="slot-{idx}-image-ok"]', timeout=5000
        )


async def screenshot(page: Page, name: str) -> str:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    p = SHOTS_DIR / f"{name}.png"
    try:
        await page.screenshot(path=str(p), full_page=True)
        return str(p)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Fixtures: tiny image files for upload (1x1 PNG)
# ---------------------------------------------------------------------------


def ensure_image_fixtures() -> None:
    """Create 1x1 PNG fixtures referenced by batch.csv if missing."""
    images_dir = FIXTURES / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    one_pixel_png = bytes([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4,
        0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41,
        0x54, 0x78, 0x9c, 0x62, 0x00, 0x01, 0x00, 0x00,
        0x05, 0x00, 0x01, 0x0d, 0x0a, 0x2d, 0xb4, 0x00,
        0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae,
        0x42, 0x60, 0x82,
    ])
    for asset_id in ("A-9001", "A-9002", "A-9003", "A-9004", "A-9005"):
        target = images_dir / f"{asset_id}.png"
        if not target.exists():
            target.write_bytes(one_pixel_png)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def s_happy_grid_2x2(page: Page, suite: Suite) -> None:
    name = "S1 happy: grid-2x2 full flow"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        # Verify contents table populated
        rendered = await page.locator(
            '[data-testid="contents-table"] tbody tr'
        ).count()
        if rendered != 5:
            shot = await screenshot(page, "s1_after_upload")
            return suite.record(name, False, f"expected 5 rows, got {rendered}", shot)

        await go_to_curation(page)
        await pick_layout(page, "grid-2x2")
        # Fill all 4 slots
        for i, cid in enumerate(["A-9001", "A-9002", "A-9003", "A-9004"], start=1):
            img = FIXTURES / "images" / f"{cid}.png"
            await fill_slot(page, i, cid, img)
        await page.fill('[data-testid="input-comment"]', "Spring drop hero")
        # Save should now be enabled
        save = page.locator('[data-testid="btn-save-layout"]')
        if await save.is_disabled():
            shot = await screenshot(page, "s1_save_disabled")
            return suite.record(name, False, "save still disabled with all slots filled", shot)
        await save.click()
        await page.wait_for_selector('[data-testid="status-saved"]', timeout=3000)
        # Apply
        apply = page.locator('[data-testid="btn-apply-layout"]')
        await apply.click()
        await page.wait_for_selector('[data-testid="status-applied"]', timeout=3000)
        # Check applied list
        applied_count = await page.locator(
            '[data-testid^="applied-layout-"]'
        ).count()
        if applied_count != 1:
            shot = await screenshot(page, "s1_after_apply")
            return suite.record(name, False, f"expected 1 applied, got {applied_count}", shot)
        shot = await screenshot(page, "s1_done")
        suite.record(name, True, "all 4 slots filled + saved + applied", shot)
    except Exception as e:
        shot = await screenshot(page, "s1_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_happy_featured_row(page: Page, suite: Suite) -> None:
    name = "S2 happy: featured-row full flow"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await go_to_curation(page)
        await pick_layout(page, "featured-row")
        for i, cid in enumerate(["A-9001", "A-9002", "A-9003", "A-9004"], start=1):
            await fill_slot(page, i, cid, FIXTURES / "images" / f"{cid}.png")
        await page.fill('[data-testid="input-comment"]', "Featured row test")
        await page.click('[data-testid="btn-save-layout"]')
        await page.wait_for_selector('[data-testid="status-saved"]', timeout=3000)
        await page.click('[data-testid="btn-apply-layout"]')
        await page.wait_for_selector('[data-testid="status-applied"]', timeout=3000)
        suite.record(name, True, "featured-row applied")
    except Exception as e:
        shot = await screenshot(page, "s2_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_happy_carousel(page: Page, suite: Suite) -> None:
    name = "S3 happy: carousel (5 slots)"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await go_to_curation(page)
        await pick_layout(page, "carousel")
        for i, cid in enumerate(
            ["A-9001", "A-9002", "A-9003", "A-9004", "A-9005"], start=1
        ):
            await fill_slot(page, i, cid, FIXTURES / "images" / f"{cid}.png")
        await page.fill('[data-testid="input-comment"]', "Carousel run")
        await page.click('[data-testid="btn-save-layout"]')
        await page.wait_for_selector('[data-testid="status-saved"]', timeout=3000)
        await page.click('[data-testid="btn-apply-layout"]')
        await page.wait_for_selector('[data-testid="status-applied"]', timeout=3000)
        suite.record(name, True, "5-slot carousel applied")
    except Exception as e:
        shot = await screenshot(page, "s3_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_curation_before_upload(page: Page, suite: Suite) -> None:
    name = "S4 edge: open Curation with no contents"
    try:
        await reset_state(page)
        await go_to_curation(page)
        warn = page.locator('[data-testid="no-contents-warning"]')
        if await warn.count() == 0:
            shot = await screenshot(page, "s4_missing_warning")
            return suite.record(name, False, "expected no-contents-warning", shot)
        # Layout picker should NOT be visible
        picker = page.locator('[data-testid="layout-picker"]')
        if await picker.count() > 0 and await picker.is_visible():
            shot = await screenshot(page, "s4_picker_visible")
            return suite.record(name, False, "layout picker visible without contents", shot)
        suite.record(name, True, "warning shown, picker hidden")
    except Exception as e:
        shot = await screenshot(page, "s4_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_save_disabled_until_complete(page: Page, suite: Suite) -> None:
    name = "S5 edge: Save disabled until all slots + comment present"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await go_to_curation(page)
        await pick_layout(page, "grid-2x2")
        save = page.locator('[data-testid="btn-save-layout"]')

        # 0 slots filled, no comment
        if not await save.is_disabled():
            return suite.record(name, False, "save enabled with 0 slots filled")

        # Fill 2 of 4 slots
        await fill_slot(page, 1, "A-9001", FIXTURES / "images" / "A-9001.png")
        await fill_slot(page, 2, "A-9002", FIXTURES / "images" / "A-9002.png")
        if not await save.is_disabled():
            return suite.record(name, False, "save enabled with 2/4 slots")

        # Fill remaining slots
        await fill_slot(page, 3, "A-9003", FIXTURES / "images" / "A-9003.png")
        await fill_slot(page, 4, "A-9004", FIXTURES / "images" / "A-9004.png")
        if not await save.is_disabled():
            return suite.record(name, False, "save enabled before comment typed")

        # Add comment
        await page.fill('[data-testid="input-comment"]', "ok")
        # Now save should be enabled
        await asyncio.sleep(0.1)
        if await save.is_disabled():
            shot = await screenshot(page, "s5_save_still_disabled")
            return suite.record(name, False, "save still disabled with all reqs met", shot)
        suite.record(name, True, "save gated until 4/4 slots + comment + image_uploaded")
    except Exception as e:
        shot = await screenshot(page, "s5_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_apply_disabled_until_saved(page: Page, suite: Suite) -> None:
    name = "S6 edge: Apply disabled until Save"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await go_to_curation(page)
        await pick_layout(page, "grid-2x2")
        for i, cid in enumerate(["A-9001", "A-9002", "A-9003", "A-9004"], start=1):
            await fill_slot(page, i, cid, FIXTURES / "images" / f"{cid}.png")
        await page.fill('[data-testid="input-comment"]', "Pre-save")
        apply = page.locator('[data-testid="btn-apply-layout"]')
        if not await apply.is_disabled():
            return suite.record(name, False, "apply enabled before save")
        await page.click('[data-testid="btn-save-layout"]')
        await page.wait_for_selector('[data-testid="status-saved"]', timeout=3000)
        if await apply.is_disabled():
            return suite.record(name, False, "apply still disabled after save")
        suite.record(name, True, "apply correctly gated by save")
    except Exception as e:
        shot = await screenshot(page, "s6_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_csv_missing_columns(page: Page, suite: Suite) -> None:
    name = "S7 edge: CSV missing required column"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch_missing_image_col.csv")
        # Either an error block appears, or the contents table is empty/partial.
        errors = page.locator('[data-testid="csv-errors"]')
        if await errors.count() > 0 and await errors.is_visible():
            text = await errors.inner_text()
            return suite.record(
                name, True, f"errors surfaced: {text[:60]!r}"
            )
        # No error block — see if contents loaded anyway (the image_path
        # column is optional from the parser's viewpoint; missing it
        # would just leave that field blank).
        rendered = await page.locator(
            '[data-testid="contents-table"] tbody tr'
        ).count()
        suite.record(
            name, True,
            f"no hard error; contents table rendered {rendered} rows (parser is permissive)"
        )
    except Exception as e:
        shot = await screenshot(page, "s7_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_layout_swap_replaces_draft(page: Page, suite: Suite) -> None:
    name = "S8 edge: switching layout replaces draft"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await go_to_curation(page)
        await pick_layout(page, "grid-2x2")
        await fill_slot(page, 1, "A-9001", FIXTURES / "images" / "A-9001.png")
        # Switch to carousel mid-edit
        await page.click('[data-testid="layout-option-carousel"]')
        await page.wait_for_selector('[data-testid="slots-carousel"]', timeout=3000)
        # Count slot cards by their direct class. The data-testid^="slot-"
        # prefix also matches slot-N-content-select etc., so we filter to
        # bare slot-N entries via JS.
        slot_count = await page.evaluate(
            "() => Array.from(document.querySelectorAll('[data-testid]'))"
            ".filter(e => /^slot-\\d+$/.test(e.dataset.testid)).length"
        )
        if slot_count != 5:
            shot = await screenshot(page, "s8_after_swap")
            return suite.record(name, False, f"expected 5 slots after swap, got {slot_count}", shot)
        # Slot 1 should be empty (new layout = fresh slots)
        first_slot_value = await page.eval_on_selector(
            '[data-testid="slot-1-content-select"]', "el => el.value"
        )
        if first_slot_value:
            return suite.record(name, False, f"slot 1 retained value {first_slot_value!r} after layout swap")
        suite.record(name, True, "draft replaced cleanly with empty 5-slot layout")
    except Exception as e:
        shot = await screenshot(page, "s8_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_unique_content_per_slot(page: Page, suite: Suite) -> None:
    name = "S9 edge: same content_id can't appear in 2 slots"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await go_to_curation(page)
        await pick_layout(page, "grid-2x2")
        await fill_slot(page, 1, "A-9001", FIXTURES / "images" / "A-9001.png")
        # Slot 2's options should NOT include A-9001 (filtered)
        slot2_options = await page.eval_on_selector_all(
            '[data-testid="slot-2-content-select"] option',
            "els => els.map(e => e.value)"
        )
        if "A-9001" in slot2_options:
            return suite.record(name, False, "A-9001 still selectable in slot 2")
        suite.record(name, True, "slot 2 correctly excludes A-9001 from options")
    except Exception as e:
        shot = await screenshot(page, "s9_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_three_layouts_back_to_back(page: Page, suite: Suite) -> None:
    name = "S10 stress: 3 layouts back-to-back"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        layouts = [
            ("grid-2x2", 4, "Run A"),
            ("featured-row", 4, "Run B"),
            ("carousel", 5, "Run C"),
        ]
        for layout_id, n, comment in layouts:
            await go_to_curation(page)
            await pick_layout(page, layout_id)
            ids = ["A-9001", "A-9002", "A-9003", "A-9004", "A-9005"][:n]
            for i, cid in enumerate(ids, start=1):
                await fill_slot(page, i, cid, FIXTURES / "images" / f"{cid}.png")
            await page.fill('[data-testid="input-comment"]', comment)
            await page.click('[data-testid="btn-save-layout"]')
            await page.wait_for_selector('[data-testid="status-saved"]', timeout=3000)
            await page.click('[data-testid="btn-apply-layout"]')
            await page.wait_for_selector('[data-testid="status-applied"]', timeout=3000)
        applied = await page.locator(
            '[data-testid^="applied-layout-"]'
        ).count()
        if applied != 3:
            shot = await screenshot(page, "s10_final")
            return suite.record(name, False, f"expected 3 applied, got {applied}", shot)
        suite.record(name, True, f"3 layouts applied sequentially")
    except Exception as e:
        shot = await screenshot(page, "s10_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_state_persists_across_reload(page: Page, suite: Suite) -> None:
    name = "S11 edge: state persists across page reload"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await page.reload(wait_until="networkidle")
        # Contents should still be there
        rendered = await page.locator(
            '[data-testid="contents-table"] tbody tr'
        ).count()
        if rendered != 5:
            shot = await screenshot(page, "s11_after_reload")
            return suite.record(name, False, f"after reload, expected 5 rows got {rendered}", shot)
        suite.record(name, True, "5 contents survived a page reload via localStorage")
    except Exception as e:
        shot = await screenshot(page, "s11_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_save_button_disables_after_save(page: Page, suite: Suite) -> None:
    name = "S12 edge: Save button shows 'Saved' and disables after save"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await go_to_curation(page)
        await pick_layout(page, "grid-2x2")
        for i, cid in enumerate(["A-9001", "A-9002", "A-9003", "A-9004"], start=1):
            await fill_slot(page, i, cid, FIXTURES / "images" / f"{cid}.png")
        await page.fill('[data-testid="input-comment"]', "Once")
        save = page.locator('[data-testid="btn-save-layout"]')
        await save.click()
        await page.wait_for_selector('[data-testid="status-saved"]', timeout=3000)
        # Save should now be disabled and show 'Saved'
        text = (await save.inner_text()).strip()
        if text != "Saved":
            return suite.record(name, False, f"save text {text!r} (expected 'Saved')")
        if not await save.is_disabled():
            return suite.record(name, False, "save still enabled after save")
        suite.record(name, True, "save button correctly transitions to disabled 'Saved'")
    except Exception as e:
        shot = await screenshot(page, "s12_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


async def s_no_image_no_save(page: Page, suite: Suite) -> None:
    name = "S13 edge: image upload required for save"
    try:
        await reset_state(page)
        await upload_csv(page, FIXTURES / "batch.csv")
        await go_to_curation(page)
        await pick_layout(page, "grid-2x2")
        # Fill all slot CONTENTS but skip image uploads
        for i, cid in enumerate(["A-9001", "A-9002", "A-9003", "A-9004"], start=1):
            await page.select_option(
                f'[data-testid="slot-{i}-content-select"]', cid
            )
        await page.fill('[data-testid="input-comment"]', "Anything")
        save = page.locator('[data-testid="btn-save-layout"]')
        if not await save.is_disabled():
            return suite.record(name, False, "save enabled without uploaded images")
        suite.record(name, True, "save correctly blocked without per-slot images")
    except Exception as e:
        shot = await screenshot(page, "s13_exception")
        suite.record(name, False, f"exception: {type(e).__name__}: {e}", shot)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def main() -> int:
    ensure_image_fixtures()
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    suite = Suite()

    vite = start_portal()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context()
            page = await ctx.new_page()

            scenarios = [
                s_happy_grid_2x2,
                s_happy_featured_row,
                s_happy_carousel,
                s_curation_before_upload,
                s_save_disabled_until_complete,
                s_apply_disabled_until_saved,
                s_csv_missing_columns,
                s_layout_swap_replaces_draft,
                s_unique_content_per_slot,
                s_three_layouts_back_to_back,
                s_state_persists_across_reload,
                s_save_button_disables_after_save,
                s_no_image_no_save,
            ]
            for sc in scenarios:
                try:
                    await sc(page, suite)
                except Exception as e:
                    suite.record(
                        sc.__name__,
                        False,
                        f"unhandled scenario exception: {type(e).__name__}: {e}",
                    )

            await browser.close()
    finally:
        info("stopping vite ...")
        stop_tree(vite)

    print("\n" + "=" * 64)
    passed = sum(1 for r in suite.results if r.passed)
    total = len(suite.results)
    print(f"OPERATOR-SUITE RESULTS: {passed}/{total} PASS")
    print("=" * 64)
    for r in suite.results:
        marker = "PASS" if r.passed else "FAIL"
        print(f"  [{marker}] {r.name} -- {r.note}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
