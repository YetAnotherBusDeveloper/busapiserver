"""Opt-in native browser regressions, using temporary accounts and no network.

RUN_MCP_BROWSER_TESTS=1 python -m pytest test_mcp_browser.py -q
Install Playwright plus Chromium first. MCP_TEST_BROWSER_EXECUTABLE optionally
selects an existing Chromium executable; no personal browser profile is used.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import unittest
from urllib.parse import parse_qs, urlsplit

import test_mcp


@unittest.skipUnless(
    os.getenv("RUN_MCP_BROWSER_TESTS") == "1"
    and importlib.util.find_spec("playwright") is not None,
    "Set RUN_MCP_BROWSER_TESTS=1 and install Playwright/Chromium",
)
class MCPBrowserConsentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        # Use full Chromium's new headless mode, matching desktop Chrome. The
        # separate headless-shell bypasses route interception on this redirect
        # and attempts DNS for the deliberately non-existent callback domain.
        options = {"headless": True, "channel": "chromium"}
        executable = os.getenv("MCP_TEST_BROWSER_EXECUTABLE")
        if executable:
            options["executable_path"] = executable
        cls.browser = cls.playwright.chromium.launch(**options)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        # The fixture has synthetic accounts, temporary databases and mocked
        # transit services. Suppress HTTP client URLs containing test OAuth data.
        previous_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_level)
        self.server = test_mcp.MCPIntegrationTests()
        self.server.setUp()
        self.addCleanup(self.server.tearDown)
        self.origin = self.server.provider.issuer
        self.posts = []
        self.callbacks = []
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.add_cookies(
            [
                {
                    "name": "yabus_auth_token",
                    "value": self.server.login.token,
                    "url": self.origin,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        )
        self.context.route("**/*", self.bridge)
        self.page = self.context.new_page()

    def bridge(self, route):
        request = route.request
        target = urlsplit(request.url)
        if target.hostname == "client.example" and target.path == "/callback":
            self.callbacks.append(
                {
                    "referer": request.headers.get("referer"),
                    "code": bool(parse_qs(target.query).get("code")),
                    "error": parse_qs(target.query).get("error", [None])[0],
                }
            )
            route.fulfill(
                status=200, content_type="text/html", body="Synthetic OAuth callback"
            )
            return
        if not request.url.startswith(self.origin + "/"):
            route.abort()
            return
        # Trust only the browser's headers/cookies. TestClient's cookie jar must
        # not conceal missing cookies or manufacture an Origin on its behalf.
        self.server.client.cookies.clear()
        response = self.server.client.request(
            request.method,
            request.url,
            headers=request.all_headers(),
            content=request.post_data_buffer,
        )
        if request.method == "POST":
            self.posts.append(
                {
                    "path": target.path,
                    "origin": request.headers.get("origin"),
                    "referer": request.headers.get("referer"),
                    "status": response.status_code,
                }
            )
        headers = dict(response.headers)
        headers.pop("content-length", None)
        route.fulfill(
            status=response.status_code, headers=headers, body=response.content
        )

    def open_consent(self):
        client = self.server.register()
        response = self.server.begin(client)
        self.assertEqual(response.status_code, 302)
        page = self.page.goto(response.headers["location"])
        self.assertEqual(page.status, 200)
        return client

    def assert_post(self, path):
        self.assertEqual(
            self.posts[-1],
            {
                "path": path,
                "origin": self.origin,
                "referer": self.origin + "/",
                "status": 303,
            },
        )

    def test_approve_with_browser_generated_origin_reaches_callback(self):
        self.open_consent()
        self.page.locator("button[value=approve]").click()
        self.page.wait_for_url("https://client.example/callback*", timeout=5000)
        self.assert_post("/oauth/consent")
        self.assertEqual(urlsplit(self.page.url).hostname, "client.example")
        self.assertEqual(
            self.callbacks, [{"referer": None, "code": True, "error": None}]
        )
        self.assertEqual(
            len(self.server.provider.account_grants(self.server.login.account_id)), 1
        )

    def test_deny_with_browser_generated_origin_reaches_callback(self):
        self.open_consent()
        self.page.locator("button[value=deny]").click()
        self.page.wait_for_url("https://client.example/callback*", timeout=5000)
        self.assert_post("/oauth/consent")
        self.assertEqual(
            self.callbacks, [{"referer": None, "code": False, "error": "access_denied"}]
        )
        self.assertEqual(
            self.server.provider.account_grants(self.server.login.account_id), []
        )

    def test_revoke_with_browser_generated_origin(self):
        client = self.server.register()
        self.server.consent(client)
        self.page.goto(self.origin + "/oauth/connections")
        self.page.locator("button").click()
        self.assert_post("/oauth/connections")
        self.assertEqual(
            self.server.provider.account_grants(self.server.login.account_id), []
        )


if __name__ == "__main__":
    unittest.main()
