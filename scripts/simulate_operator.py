"""Playwright operator simulator for the rebuilt sample portal.

Drives the upload -> curation -> save -> apply flow exactly as a human
operator would. Used in Phase 1 to verify the portal works, in Phase 3
*alongside* `pilot.teach` so the runner records the operator's actions
into a replayable skill.

Usage:
    .venv/bin/python scripts/simulate_operator.py \
        --csv tests/fixtures/batch.csv \
        --layout featured-row \
        --comment "Spring drop hero" \
        --portal-url http://localhost:5173 \
        --headless
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from playwright.async_api import async_playwright


REPO_ROOT = Path(__file__).resolve().parent.parent


async def run(args: argparse.Namespace) -> int:
    csv_path = (REPO_ROOT / args.csv).resolve()
    if not csv_path.exists():
        print(f"FATAL: csv not found: {csv_path}", file=sys.stderr)
        return 2

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("FATAL: csv is empty", file=sys.stderr)
        return 2

    layout_slot_count = {"grid-2x2": 4, "featured-row": 4, "carousel": 5}
    if args.layout not in layout_slot_count:
        print(f"FATAL: unknown layout: {args.layout}", file=sys.stderr)
        return 2
    needed = layout_slot_count[args.layout]
    if len(rows) < needed:
        print(
            f"FATAL: layout {args.layout} needs {needed} contents, csv has {len(rows)}",
            file=sys.stderr,
        )
        return 2

    print(f"[op] using {len(rows)} contents from {csv_path.name}")
    print(f"[op] target layout: {args.layout} ({needed} slots)")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=args.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context()
        page = await ctx.new_page()

        await page.goto(args.portal_url, wait_until="networkidle")
        # Reset prior local-storage state so reruns are deterministic.
        await page.evaluate("() => window.localStorage.clear()")
        await page.reload(wait_until="networkidle")

        # ---- Upload tab ----
        await page.click('[data-testid="nav-upload"]')
        await page.wait_for_selector('[data-testid="upload-page"]')
        await page.set_input_files('[data-testid="input-csv-file"]', str(csv_path))
        await page.wait_for_selector('[data-testid="contents-table"]')
        rendered_rows = await page.locator(
            '[data-testid="contents-table"] tbody tr'
        ).count()
        assert rendered_rows == len(rows), (
            f"expected {len(rows)} rendered rows, got {rendered_rows}"
        )
        print(f"[op] uploaded csv; {rendered_rows} rows visible")

        # ---- Curation tab ----
        await page.click('[data-testid="nav-curation"]')
        await page.wait_for_selector('[data-testid="curation-page"]')
        await page.click(f'[data-testid="layout-option-{args.layout}"]')
        await page.wait_for_selector('[data-testid="layout-editor"]')
        print(f"[op] selected layout {args.layout}")

        # Fill slots one by one.
        for slot_idx in range(1, needed + 1):
            row = rows[slot_idx - 1]
            content_id = row["content_id"]
            image_abspath = (REPO_ROOT / row["image_path"]).resolve()
            if not image_abspath.exists():
                print(
                    f"FATAL: image missing for {content_id}: {image_abspath}",
                    file=sys.stderr,
                )
                return 2
            await page.select_option(
                f'[data-testid="slot-{slot_idx}-content-select"]',
                value=content_id,
            )
            await page.set_input_files(
                f'[data-testid="slot-{slot_idx}-image-input"]',
                str(image_abspath),
            )
            await page.wait_for_selector(f'[data-testid="slot-{slot_idx}-image-ok"]')
            print(f"[op]  slot {slot_idx} <- {content_id} (+image)")

        # Comment + save + apply.
        await page.fill('[data-testid="input-comment"]', args.comment)
        await page.click('[data-testid="btn-save-layout"]')
        await page.wait_for_selector('[data-testid="status-saved"]')
        print("[op] saved")
        await page.click('[data-testid="btn-apply-layout"]')
        await page.wait_for_selector('[data-testid="status-applied"]')
        print("[op] applied")

        # Final assertion: applied list shows the layout we just curated.
        applied_count = await page.locator(
            '[data-testid="applied-layouts"] li'
        ).count()
        assert applied_count >= 1, f"no applied layouts shown: {applied_count}"
        print(f"[op] applied list shows {applied_count} layout(s); SUCCESS")

        if args.screenshot:
            shot = REPO_ROOT / args.screenshot
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), full_page=True)
            print(f"[op] screenshot saved: {shot}")

        await ctx.close()
        await browser.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--layout", required=True,
                   choices=["grid-2x2", "featured-row", "carousel"])
    p.add_argument("--comment", default="auto-generated comment")
    p.add_argument("--portal-url", default="http://localhost:5173")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--no-headless", dest="headless", action="store_false")
    p.add_argument("--screenshot", default=None,
                   help="optional path to save a final screenshot")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
