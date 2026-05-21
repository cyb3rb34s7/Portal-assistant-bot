"""Replay idempotency shim regression coverage."""

from __future__ import annotations

from playwright.sync_api import sync_playwright

from pilot.skill_runner import SkillRunner


def test_xhr_respects_existing_custom_idempotency_header() -> None:
    """Configured header_name drives XHR existing-header detection."""
    captured_headers: list[dict[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def handler(route):
            if route.request.url.endswith("/api/custom"):
                captured_headers.append(route.request.headers)
                route.fulfill(status=200, content_type="application/json", body="{}")
                return
            route.fulfill(status=200, content_type="text/html", body="<html></html>")

        page.route("**/*", handler)
        page.goto("http://fixture.test/")
        page.evaluate(SkillRunner._IDEMPOTENCY_INSTALL_JS)
        page.evaluate(
            "(cfg) => { window.__cp_idem_config = cfg; }",
            {
                "enabled": True,
                "header_name": "X-CP-Idempotency",
                "endpoint_patterns": ["/api/custom"],
                "method_patterns": ["POST"],
                "key_components": ["session", "step_index", "method", "url_path"],
                "body_hash_fields": [],
                "allow_existing_header": True,
                "session_id": "f03",
                "step_index": 3,
            },
        )

        page.evaluate(
            """() => new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/custom');
                xhr.setRequestHeader('X-CP-Idempotency', 'app-owned-key');
                xhr.onload = () => resolve(xhr.responseText);
                xhr.onerror = () => reject(new Error('xhr failed'));
                xhr.send('body');
            })"""
        )

        browser.close()

    assert len(captured_headers) == 1
    assert captured_headers[0]["x-cp-idempotency"] == "app-owned-key"
