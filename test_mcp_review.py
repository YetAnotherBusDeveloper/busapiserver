from __future__ import annotations

import json
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import test_mcp
from app.account_sync_service import get_sync_document
from app.auth_service import authenticate_token
from app.db import get_connection
from app.mcp_auth import CSRF_COOKIE, SESSION_COOKIE, digest
from app.mcp_review import (
    BOOTSTRAP_FILENAME,
    _hash_password,
    active_review_account,
    bootstrap_review_account,
    disable_review_account,
    provision_review_account,
)


class MCPReviewLoginTests(unittest.TestCase):
    def setUp(self):
        self.server = test_mcp.MCPIntegrationTests()
        self.server.setUp()
        self.addCleanup(self.server.tearDown)
        self.settings = self.server.settings
        self.client = self.server.client
        self.password = "synthetic-review-password-12345"
        self.username = "yabus-review"

    def provision(self, password=None):
        return provision_review_account(
            self.settings, self.username, password or self.password
        )

    def bootstrap_file(self, *, password=None, enabled=True):
        config = {
            "username": self.username,
            "password_hash": _hash_password(password or self.password),
            "enabled": enabled,
        }
        path = self.settings.project_dir / BOOTSTRAP_FILENAME
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def form(self, pending=None):
        path = "/oauth/review-login"
        if pending:
            path += "?" + urlencode({"request_id": pending})
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        csrf = re.search("name='csrf' value='([^']+)'", response.text)[1]
        return {
            "csrf": csrf,
            "username": self.username,
            "password": self.password,
            "request_id": pending or "",
        }

    def post(self, form, **kwargs):
        return self.client.post(
            "/oauth/review-login",
            data=form,
            headers={"Origin": self.server.provider.issuer},
            **kwargs,
        )

    def grant(self):
        app_client = self.server.register()
        begin = self.server.begin(app_client)
        pending = parse_qs(urlsplit(begin.headers["location"]).query)["request_id"][0]
        signed_in = self.post(self.form(pending))
        self.assertEqual(signed_in.status_code, 303)
        consent = self.client.get(signed_in.headers["location"])
        self.assertIn("YABus 示範帳號", consent.text)
        csrf = re.search("name='csrf' value='([^']+)'", consent.text)[1]
        response = self.client.post(
            "/oauth/consent",
            data={"csrf": csrf, "request_id": pending, "decision": "approve"},
            headers={"Origin": self.server.provider.issuer},
        )
        self.assertEqual(response.status_code, 303)
        code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
        token_response = self.server.exchange(app_client, code)
        self.assertEqual(token_response.status_code, 200)
        return app_client, token_response.json()

    def test_not_provisioned_is_disabled(self):
        self.assertEqual(self.client.get("/oauth/review-login").status_code, 404)
        begin = self.server.begin(self.server.register())
        response = self.client.get(begin.headers["location"])
        self.assertNotIn("/oauth/review-login", response.text)
        self.assertIn("Google", response.text)

    def test_provision_creates_only_unlinked_sample_account_and_hash(self):
        account_id = self.provision()
        self.assertNotIn(
            account_id, (self.server.login.account_id, self.server.other.account_id)
        )
        with get_connection(self.settings.app_db_path) as db:
            row = db.execute("SELECT * FROM mcp_review_account").fetchone()
            self.assertNotIn(self.password, row["password_hash"])
            self.assertTrue(row["password_hash"].startswith("pbkdf2_sha256$600000$"))
            self.assertEqual(
                db.execute(
                    "SELECT role FROM accounts WHERE id=?", (account_id,)
                ).fetchone()[0],
                "user",
            )
            for table in (
                "account_oauth_identities",
                "account_devices",
                "account_device_tokens",
            ):
                self.assertEqual(
                    db.execute(
                        f"SELECT count(*) FROM {table} WHERE account_id=?",
                        (account_id,),
                    ).fetchone()[0],
                    0,
                )
        document = get_sync_document(
            self.settings, account_id=account_id, namespace="favorites"
        )
        self.assertEqual(
            [x["rawStopId"] for x in document.payload["groups"]["送審示範"]],
            ["194687", "194688"],
        )
        self.assertEqual(len(document.payload["groups"]["測試其他"]), 1)
        alice = get_sync_document(
            self.settings,
            account_id=self.server.login.account_id,
            namespace="favorites",
        )
        self.assertEqual(set(alice.payload["groups"]), {"上班"})

    def test_login_session_cannot_be_used_as_application_token(self):
        account_id = self.provision()
        response = self.post(self.form())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/oauth/connections")
        session = self.client.cookies.get(SESSION_COOKIE)
        self.assertIsNone(authenticate_token(self.settings, session))
        headers = response.headers.get_list("set-cookie")
        self.assertTrue(
            any(
                "Secure" in h and "HttpOnly" in h and "Path=/oauth" in h
                for h in headers
                if SESSION_COOKIE in h
            )
        )
        self.assertNotIn(self.password, response.text + response.headers["location"])
        with get_connection(self.settings.app_db_path) as db:
            row = db.execute(
                "SELECT * FROM mcp_login_sessions WHERE hash=?", (digest(session),)
            ).fetchone()
            self.assertEqual(row["account_id"], account_id)
            self.assertNotEqual(row["hash"], session)

    def test_complete_pkce_consent_reads_only_demo_favorites(self):
        self.provision()
        _, tokens = self.grant()
        result = self.server.rpc(
            "/mcp",
            "tools/call",
            {"name": "get_my_favorites", "arguments": {"group": "送審示範"}},
            token=tokens["access_token"],
        )
        data = result.json()["result"]["structuredContent"]
        self.assertTrue(data["has_data"])
        self.assertEqual(set(data["groups"]), {"送審示範"})
        self.assertEqual(len(data["groups"]["送審示範"]), 2)
        self.assertNotIn("Private Bob", result.text)
        denied = self.server.rpc(
            "/mcp",
            "tools/call",
            {"name": "get_my_favorites", "arguments": {"group": "上班"}},
            token=tokens["access_token"],
        )
        self.assertTrue(denied.json()["result"]["isError"])

    def test_invalid_credentials_csrf_and_rate_limit(self):
        self.provision()
        form = self.form()
        with patch("app.mcp_auth.create_review_session") as verifier:
            invalid = self.client.post(
                "/oauth/review-login",
                data=form,
                headers={"Origin": "https://other.example"},
            )
            self.assertEqual(invalid.status_code, 403)
            verifier.assert_not_called()
        for username, password in (
            ("other", self.password),
            (self.username, "wrong-password-long-enough"),
        ):
            response = self.post({**form, "username": username, "password": password})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(password, response.text)
            self.assertNotIn(SESSION_COOKIE, response.headers.get("set-cookie", ""))
            form["csrf"] = self.client.cookies.get(CSRF_COOKIE)
        for _ in range(3):
            response = self.post({**form, "password": "wrong-password-long-enough"})
            self.assertEqual(response.status_code, 401)
            form["csrf"] = self.client.cookies.get(CSRF_COOKIE)
        self.assertEqual(self.post(form).status_code, 429)

    def test_password_rotation_and_disable_revoke_existing_access(self):
        account_id = self.provision()
        _, tokens = self.grant()
        self.assertEqual(
            self.provision("new-synthetic-review-password-12345"), account_id
        )
        self.assertEqual(
            self.server.rpc(
                "/mcp", "tools/list", token=tokens["access_token"]
            ).status_code,
            401,
        )
        old = self.post(self.form())
        self.assertEqual(old.status_code, 401)
        self.password = "new-synthetic-review-password-12345"
        _, tokens = self.grant()
        self.assertEqual(self.post(self.form()).status_code, 303)
        disable_review_account(self.settings)
        self.assertEqual(self.client.get("/oauth/review-login").status_code, 404)
        self.assertEqual(
            self.server.rpc(
                "/mcp", "tools/list", token=tokens["access_token"]
            ).status_code,
            401,
        )
        self.assertIn("請先登入", self.client.get("/oauth/connections").text)

    def test_privileged_or_linked_demo_identity_fails_closed(self):
        account_id = self.provision()
        with get_connection(self.settings.app_db_path) as db:
            db.execute("UPDATE accounts SET role='admin' WHERE id=?", (account_id,))
            db.commit()
        self.assertIsNone(active_review_account(self.settings))
        self.assertEqual(self.client.get("/oauth/review-login").status_code, 404)
        with self.assertRaises(ValueError):
            self.provision()
        with get_connection(self.settings.app_db_path) as db:
            db.execute("UPDATE accounts SET role='user' WHERE id=?", (account_id,))
            db.execute(
                "INSERT INTO account_oauth_identities "
                "(provider,provider_user_id,account_id,created_at,updated_at) "
                "VALUES('google','must-not-be-a-demo-login',?,0,0)",
                (account_id,),
            )
            db.commit()
        self.assertIsNone(active_review_account(self.settings))
        self.assertEqual(self.client.get("/oauth/review-login").status_code, 404)
        with self.assertRaises(ValueError):
            self.provision()

    def test_expired_request_and_account_override_do_not_create_access(self):
        demo_id = self.provision()
        invalid = self.client.get("/oauth/review-login?request_id=expired")
        self.assertEqual(invalid.status_code, 400)
        form = self.form()
        response = self.post(
            {
                **form,
                "account_id": str(self.server.login.account_id),
                "next": "https://other.example/",
                "redirect_uri": "https://other.example/",
            }
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/oauth/connections")
        with get_connection(self.settings.app_db_path) as db:
            account_id = db.execute(
                "SELECT account_id FROM mcp_login_sessions WHERE hash=?",
                (digest(self.client.cookies.get(SESSION_COOKIE)),),
            ).fetchone()[0]
        self.assertEqual(account_id, demo_id)

    def test_credentials_changed_during_password_check_cannot_issue_session(self):
        self.provision()
        with patch(
            "app.mcp_review._verify_password",
            side_effect=lambda *a: disable_review_account(self.settings) or True,
        ):
            response = self.post(self.form())
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(SESSION_COOKIE, response.headers.get("set-cookie", ""))

    def test_missing_bootstrap_file_keeps_demo_login_disabled(self):
        self.assertIsNone(bootstrap_review_account(self.settings))
        self.assertIsNone(active_review_account(self.settings))

    def test_bootstrap_is_idempotent_and_preserves_live_grants(self):
        self.bootstrap_file()
        account_id = bootstrap_review_account(self.settings)
        _, tokens = self.grant()
        original = get_sync_document(
            self.settings, account_id=account_id, namespace="favorites"
        )
        with patch("app.mcp_review.upsert_sync_document") as write_document:
            self.assertEqual(bootstrap_review_account(self.settings), account_id)
            write_document.assert_not_called()
        current = get_sync_document(
            self.settings, account_id=account_id, namespace="favorites"
        )
        self.assertEqual(current, original)
        self.assertEqual(
            self.server.rpc(
                "/mcp", "tools/list", token=tokens["access_token"]
            ).status_code,
            200,
        )

    def test_bootstrap_password_change_and_disable_revoke_grants(self):
        self.bootstrap_file()
        account_id = bootstrap_review_account(self.settings)
        _, tokens = self.grant()
        replacement = "replacement-bootstrap-password-12345"
        self.bootstrap_file(password=replacement)
        self.assertEqual(bootstrap_review_account(self.settings), account_id)
        self.assertEqual(
            self.server.rpc(
                "/mcp", "tools/list", token=tokens["access_token"]
            ).status_code,
            401,
        )
        self.password = replacement
        _, tokens = self.grant()
        self.bootstrap_file(enabled=False)
        self.assertIsNone(bootstrap_review_account(self.settings))
        self.assertIsNone(active_review_account(self.settings))
        self.assertEqual(
            self.server.rpc(
                "/mcp", "tools/list", token=tokens["access_token"]
            ).status_code,
            401,
        )

    def test_malformed_bootstrap_does_not_leak_values_or_create_account(self):
        path = self.settings.project_dir / BOOTSTRAP_FILENAME
        for value in (
            '{"password_hash":"private-marker",',
            '{"username":"demo","password":"private-marker","enabled":true}',
            '{"username":"demo","password_hash":"private-marker","enabled":true}',
            "private-marker" * 500,
        ):
            path.write_text(value, encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                bootstrap_review_account(self.settings)
            self.assertNotIn("private-marker", str(raised.exception))
        self.assertIsNone(active_review_account(self.settings))

    def test_concurrent_bootstrap_creates_one_demo_account(self):
        self.bootstrap_file()
        with ThreadPoolExecutor(max_workers=2) as workers:
            accounts = list(workers.map(bootstrap_review_account, [self.settings] * 2))
        self.assertEqual(accounts[0], accounts[1])
        with get_connection(self.settings.app_db_path) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM mcp_review_account").fetchone()[0], 1
            )
        self.assertIsNotNone(active_review_account(self.settings))

    def test_normal_mcp_startup_applies_bootstrap_file(self):
        from contextlib import asynccontextmanager

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.mcp_server import install_mcp

        self.bootstrap_file()

        @asynccontextmanager
        async def lifespan(app):
            async with mcp_lifespan():
                yield

        app = FastAPI(lifespan=lifespan)
        app.state.settings = self.settings
        mcp_lifespan = install_mcp(app, self.settings)
        with TestClient(app, base_url=self.settings.auth_public_base_url) as client:
            response = client.get("/oauth/review-login")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(self.password, response.text)
            self.assertIsNotNone(active_review_account(self.settings))


if __name__ == "__main__":
    unittest.main()
