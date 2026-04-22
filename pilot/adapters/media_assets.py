"""Media Assets adapter for the sample portal.

Drives the Media Assets page via deterministic Playwright locators
(Level 1 only in this POC). Every method is MCP-ready: primitive inputs,
ToolResult return, plain-English docstring, no hidden side effects.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import TimeoutError as PWTimeoutError

from ..models import ToolResult
from .base import BaseAdapter


class MediaAssetsAdapter(BaseAdapter):
    name = "media_assets"

    # ---- Navigation ---------------------------------------------------------

    def navigate_to_media_assets(self) -> ToolResult:
        """
        Opens the Media Assets page via the left sidebar.
        Inputs: none.
        Success condition: the Media Assets page is rendered
        (data-testid="page-media-assets" is visible).
        """
        try:
            if self.base_url and self.base_url not in self.page.url:
                self.page.goto(self.base_url, wait_until="domcontentloaded")

            self.page.get_by_test_id("nav-media-assets").click()
            self.page.wait_for_selector(
                "[data-testid='page-media-assets']", timeout=5000
            )
            shot = self._screenshot("navigate_to_media_assets_after")
            return ToolResult(
                success=True,
                action_taken="Navigated to Media Assets via sidebar",
                screenshot_path=shot,
            )
        except Exception as e:
            shot = self._screenshot("navigate_to_media_assets_error")
            return ToolResult(
                success=False,
                action_taken="Attempted to navigate to Media Assets",
                error=str(e),
                screenshot_path=shot,
            )

    # ---- Read ---------------------------------------------------------------

    def list_assets(self) -> ToolResult:
        """
        Lists all assets visible in the Media Assets table.
        Inputs: none.
        Success condition: returns a list of {id, title, type, status, updated}
        entries matching the rows currently rendered.
        """
        try:
            self.page.wait_for_selector(
                "[data-testid='assets-table']", timeout=5000
            )
            rows = self.page.locator(
                "[data-testid='assets-table'] tbody tr"
            ).all()
            assets: list[dict[str, Any]] = []
            for row in rows:
                cells = row.locator("td").all()
                if len(cells) < 5:
                    continue
                assets.append(
                    {
                        "id": cells[0].inner_text().strip(),
                        "title": cells[1].inner_text().strip(),
                        "type": cells[2].inner_text().strip(),
                        "status": cells[3].inner_text().strip(),
                        "updated": cells[4].inner_text().strip(),
                    }
                )
            shot = self._screenshot("list_assets")
            return ToolResult(
                success=True,
                action_taken=f"Read {len(assets)} asset rows",
                output={"assets": assets},
                screenshot_path=shot,
            )
        except Exception as e:
            shot = self._screenshot("list_assets_error")
            return ToolResult(
                success=False,
                action_taken="Attempted to list assets",
                error=str(e),
                screenshot_path=shot,
            )

    def verify_asset_exists(self, asset_id: str) -> ToolResult:
        """
        Verifies that an asset with the given id appears in the table.
        Inputs: asset_id (string).
        Success condition: a row with data-testid=f"asset-row-{asset_id}"
        is visible in the table.
        """
        try:
            locator = self.page.locator(f"[data-testid='asset-row-{asset_id}']")
            visible = locator.first.is_visible()
            shot = self._screenshot(f"verify_asset_exists_{asset_id}")
            if visible:
                return ToolResult(
                    success=True,
                    action_taken=f"Confirmed asset {asset_id} is present",
                    output={"asset_id": asset_id, "exists": True},
                    screenshot_path=shot,
                )
            return ToolResult(
                success=False,
                action_taken=f"Asset {asset_id} not found in table",
                output={"asset_id": asset_id, "exists": False},
                screenshot_path=shot,
            )
        except Exception as e:
            shot = self._screenshot(f"verify_asset_exists_error_{asset_id}")
            return ToolResult(
                success=False,
                action_taken=f"Attempted to verify asset {asset_id}",
                error=str(e),
                screenshot_path=shot,
            )

    # ---- Write --------------------------------------------------------------

    def add_asset(
        self,
        asset_id: str,
        title: str,
        asset_type: str = "video",
        description: str = "",
    ) -> ToolResult:
        """
        Adds a new asset through the Add Asset modal.
        Inputs: asset_id, title, asset_type (video|image|audio), description.
        Success condition: the modal closes and the success banner reads
        "Asset {asset_id} created successfully.".
        """
        try:
            self.page.get_by_test_id("btn-add-asset").click()
            self.page.wait_for_selector(
                "[data-testid='add-asset-modal']", timeout=5000
            )

            self.page.get_by_test_id("input-asset-id").fill(asset_id)
            self.page.get_by_test_id("input-asset-title").fill(title)
            self.page.get_by_test_id("input-asset-type").select_option(
                asset_type
            )
            if description:
                self.page.get_by_test_id("input-asset-description").fill(
                    description
                )

            self._screenshot(f"add_asset_before_save_{asset_id}")
            self.page.get_by_test_id("btn-save-asset").click()

            try:
                self.page.wait_for_selector(
                    "[data-testid='add-asset-modal']",
                    state="detached",
                    timeout=3000,
                )
            except PWTimeoutError:
                # Modal still open — likely a validation error.
                err_locator = self.page.locator("[data-testid='form-error']")
                err_text = (
                    err_locator.inner_text() if err_locator.count() else ""
                )
                shot = self._screenshot(f"add_asset_validation_{asset_id}")
                return ToolResult(
                    success=False,
                    action_taken=f"Attempted to add asset {asset_id}",
                    error=f"Form error: {err_text or 'modal did not close'}",
                    screenshot_path=shot,
                )

            banner = self.page.locator("[data-testid='status-banner']")
            banner_text = banner.inner_text() if banner.count() else ""
            expected = f"Asset {asset_id} created successfully."
            shot = self._screenshot(f"add_asset_after_{asset_id}")
            if expected in banner_text:
                return ToolResult(
                    success=True,
                    action_taken=f"Added asset {asset_id}",
                    output={"asset_id": asset_id, "title": title},
                    screenshot_path=shot,
                )
            return ToolResult(
                success=False,
                action_taken=f"Add attempted for asset {asset_id}",
                error=(
                    "Success banner not found. "
                    f"Observed banner: {banner_text!r}"
                ),
                screenshot_path=shot,
            )
        except Exception as e:
            shot = self._screenshot(f"add_asset_error_{asset_id}")
            return ToolResult(
                success=False,
                action_taken=f"Attempted to add asset {asset_id}",
                error=str(e),
                screenshot_path=shot,
            )

    def delete_asset(self, asset_id: str) -> ToolResult:
        """
        Deletes the asset with the given id by clicking its Delete button.
        This action is irreversible — the runner should always place a
        human approval gate before invoking this method.
        Inputs: asset_id.
        Success condition: the row disappears and the banner reads
        "Asset {asset_id} deleted.".
        """
        try:
            button = self.page.get_by_test_id(f"btn-delete-{asset_id}")
            if button.count() == 0:
                shot = self._screenshot(f"delete_not_found_{asset_id}")
                return ToolResult(
                    success=False,
                    action_taken=f"Delete attempted for asset {asset_id}",
                    error=f"Delete button for {asset_id} not found",
                    screenshot_path=shot,
                )
            button.click()

            self.page.wait_for_selector(
                f"[data-testid='asset-row-{asset_id}']",
                state="detached",
                timeout=3000,
            )
            banner = self.page.locator("[data-testid='status-banner']")
            banner_text = banner.inner_text() if banner.count() else ""
            expected = f"Asset {asset_id} deleted."
            shot = self._screenshot(f"delete_after_{asset_id}")
            if expected in banner_text:
                return ToolResult(
                    success=True,
                    action_taken=f"Deleted asset {asset_id}",
                    output={"asset_id": asset_id},
                    screenshot_path=shot,
                )
            return ToolResult(
                success=False,
                action_taken=f"Delete attempted for asset {asset_id}",
                error=(
                    "Expected success banner not found. "
                    f"Observed banner: {banner_text!r}"
                ),
                screenshot_path=shot,
            )
        except Exception as e:
            shot = self._screenshot(f"delete_error_{asset_id}")
            return ToolResult(
                success=False,
                action_taken=f"Attempted to delete asset {asset_id}",
                error=str(e),
                screenshot_path=shot,
            )

    # ---- Search -------------------------------------------------------------

    def search_assets(self, query: str) -> ToolResult:
        """
        Types into the search input and returns the filtered asset list.
        Inputs: query (string, may be empty to clear the filter).
        Success condition: the search input reflects the query and
        list_assets() is returned for the filtered view.
        """
        try:
            search = self.page.get_by_test_id("asset-search")
            search.fill(query)
            self.page.wait_for_timeout(150)  # debounce room
            result = self.list_assets()
            # Re-tag the action for clarity
            result.action_taken = f"Searched assets for {query!r}"
            return result
        except Exception as e:
            shot = self._screenshot("search_assets_error")
            return ToolResult(
                success=False,
                action_taken=f"Attempted to search for {query!r}",
                error=str(e),
                screenshot_path=shot,
            )
