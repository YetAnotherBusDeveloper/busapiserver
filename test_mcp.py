from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.account_sync_service import upsert_sync_document
from app.auth_service import OAuthIdentity, _upsert_login
from app.db import get_connection, init_app_db, init_db
from app.mcp_auth import SCOPE, digest
from app.mcp_server import install_mcp
from app.rate_limit import reset_rate_limit_state
from test_account_sync_api import _settings


class MCPIntegrationTests(unittest.TestCase):
    def setUp(self):
        reset_rate_limit_state()
        self.directory = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.directory.name) / "bus.db")
        init_db(self.settings.db_path)
        init_app_db(self.settings.app_db_path)

        @asynccontextmanager
        async def lifespan(app):
            async with mcp_lifespan():
                yield

        self.app = FastAPI(lifespan=lifespan)
        self.app.state.settings = self.settings
        self.app.state.realtime_service = SimpleNamespace(
            get_snapshot=lambda *args, **kwargs: {
                "routeid": "TPE307",
                "updated_at": 123,
                "paths": [{"pathid": 0, "stops": [{"stopid": "S1", "sec": 180}]}],
            }
        )
        mcp_lifespan = install_mcp(self.app, self.settings)
        self.client = TestClient(
            self.app,
            base_url=self.settings.auth_public_base_url,
            follow_redirects=False,
        )
        self.client.__enter__()
        self.provider = self.app.state.mcp_provider
        self.login = self.account("alice")
        self.other = self.account("bob")
        for login, name in ((self.login, "上班"), (self.other, "Private Bob")):
            upsert_sync_document(
                self.settings,
                account_id=login.account_id,
                namespace="favorites",
                schema_version=2,
                payload={
                    "groups": {
                        name: [
                            {
                                "type": "route",
                                "provider": "tpe",
                                "routeKey": 1,
                                "routeId": "TPE307",
                                "routeName": "307",
                            }
                        ]
                    },
                    "groupKinds": {name: "route"},
                },
                client_modified_at="2026-11-06T00:00:00Z",
                conflict_policy="abort",
            )
        self.verifier = "v" * 64
        self.challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def account(self, name):
        return _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id=name,
                email=name + "@example.test",
                display_name=name,
                avatar_url=None,
            ),
            device_key=name + "-device",
            redirect_uri=self.settings.auth_public_base_url + "/account",
        )

    def register(self, method="none", **overrides):
        metadata = {
            "client_name": "Test AI",
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": SCOPE,
            **overrides,
        }
        response = self.client.post("/register", json=metadata)
        self.assertEqual(response.status_code, 201)
        return response.json()

    def begin(self, client, **overrides):
        params = {
            "client_id": client["client_id"],
            "redirect_uri": client["redirect_uris"][0],
            "response_type": "code",
            "scope": SCOPE,
            "state": "state-test",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "resource": self.provider.resource,
            **overrides,
        }
        return self.client.get("/authorize", params=params)

    def consent(self, client, decision="approve"):
        response = self.begin(client)
        self.assertEqual(response.status_code, 302)
        self.client.cookies.set("yabus_auth_token", self.login.token)
        consent = self.client.get(response.headers["location"])
        self.assertEqual(consent.status_code, 200)
        pending = parse_qs(urlsplit(response.headers["location"]).query)["request_id"][
            0
        ]
        csrf = re.search("name='csrf' value='([^']+)'", consent.text)[1]
        result = self.client.post(
            "/oauth/consent",
            data={"request_id": pending, "csrf": csrf, "decision": decision},
            headers={"Origin": self.provider.issuer},
        )
        self.assertEqual(result.status_code, 303)
        return parse_qs(urlsplit(result.headers["location"]).query), pending, csrf

    def exchange(self, client, code, **overrides):
        return self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client["client_id"],
                "code": code,
                "redirect_uri": client["redirect_uris"][0],
                "code_verifier": self.verifier,
                "resource": self.provider.resource,
                **overrides,
            },
        )

    def rpc(self, path, method, params=None, token=None):
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        return self.client.post(
            path,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers=headers,
        )

    def test_public_transport_discovery_queries_and_private_boundary(self):
        result = self.rpc(
            "/mcp/public",
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn("tools", result.json()["result"]["capabilities"])
        tools = self.rpc("/mcp/public", "tools/list").json()["result"]["tools"]
        self.assertIn("get_arrivals", [t["name"] for t in tools])
        self.assertNotIn("get_my_favorites", [t["name"] for t in tools])
        result = self.rpc(
            "/mcp/public",
            "tools/call",
            {
                "name": "get_arrivals",
                "arguments": {"route_id": "TPE307", "path_id": 0, "stop_id": "S1"},
            },
        )
        self.assertEqual(
            result.json()["result"]["structuredContent"]["data"]["paths"][0]["stops"][
                0
            ]["sec"],
            180,
        )
        denied = self.rpc("/mcp", "tools/list")
        self.assertEqual(denied.status_code, 401)
        self.assertIn("oauth-protected-resource", denied.headers["www-authenticate"])
        self.assertEqual(
            self.rpc("/mcp", "tools/list", token=self.login.token).status_code, 401
        )

    def test_oauth_favorites_isolation_refresh_persistence_and_revoke(self):
        client = self.register()
        query, _, _ = self.consent(client)
        self.assertEqual(query["state"], ["state-test"])
        response = self.exchange(client, query["code"][0])
        self.assertEqual(response.status_code, 200)
        tokens = response.json()
        result = self.rpc(
            "/mcp",
            "tools/call",
            {"name": "get_my_favorites", "arguments": {}},
            tokens["access_token"],
        )
        groups = result.json()["result"]["structuredContent"]["groups"]
        self.assertEqual(list(groups), ["上班"])
        self.assertEqual(self.exchange(client, query["code"][0]).status_code, 400)
        # Credential persistence works across provider instances/restarts.
        clone = type(self.provider)(self.settings)
        self.assertIsNotNone(
            asyncio.run(clone.load_access_token(tokens["access_token"]))
        )
        refreshed = self.client.post(
            "/token",
            data={
                "client_id": client["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertNotEqual(refreshed.json()["refresh_token"], tokens["refresh_token"])
        revoked = self.client.post(
            "/revoke",
            data={
                "client_id": client["client_id"],
                "token": refreshed.json()["refresh_token"],
            },
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(
            self.rpc(
                "/mcp", "tools/list", token=refreshed.json()["access_token"]
            ).status_code,
            401,
        )

    def test_pkce_redirect_resource_scope_csrf_and_denial(self):
        client = self.register()
        response = self.begin(client, redirect_uri="https://evil.example/callback")
        self.assertEqual(response.status_code, 400)
        for overrides in (
            {"resource": "https://evil.example/mcp"},
            {"scope": "favorites:write"},
        ):
            response = self.begin(client, **overrides)
            self.assertIn("error=", response.headers.get("location", ""))
        query, pending, csrf = self.consent(client)
        self.assertEqual(
            self.exchange(client, query["code"][0], code_verifier="x" * 64).status_code,
            400,
        )
        self.assertEqual(
            self.exchange(
                client, query["code"][0], resource="https://evil.example"
            ).status_code,
            401,
        )
        self.assertEqual(self.exchange(client, query["code"][0]).status_code, 200)
        replay = self.client.post(
            "/oauth/consent",
            data={"request_id": pending, "csrf": csrf, "decision": "approve"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(replay.status_code, 403)
        denial, _, _ = self.consent(client, "deny")
        self.assertEqual(denial["error"], ["access_denied"])
        self.assertNotIn("code", denial)

    def test_secret_hashed_at_rest_and_basic_auth(self):
        client = self.register("client_secret_basic")
        with get_connection(self.settings.app_db_path) as db:
            stored = db.execute(
                "SELECT metadata FROM mcp_clients WHERE client_id=?",
                (client["client_id"],),
            ).fetchone()[0]
        self.assertNotIn(client["client_secret"], stored)
        self.assertIn(digest(client["client_secret"]), stored)
        query, _, _ = self.consent(client)
        response = self.client.post(
            "/token",
            auth=(client["client_id"], client["client_secret"]),
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "code_verifier": self.verifier,
                "redirect_uri": client["redirect_uris"][0],
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_metadata_invalid_registration_and_payload_limits(self):
        metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertIn("none", metadata["token_endpoint_auth_methods_supported"])
        resource = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(resource["resource"], self.provider.resource)
        for uri in (
            "https://example.test/callback#fragment",
            "http://evil.example",
            "javascript:alert(1)",
        ):
            response = self.client.post(
                "/register",
                json={
                    "redirect_uris": [uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                },
            )
            self.assertEqual(response.status_code, 400)
        response = self.client.post("/register", content=b"x" * 65537)
        self.assertEqual(response.status_code, 413)
        for invalid in (b"not json", b"[]", b"null"):
            self.assertEqual(
                self.client.post("/register", content=invalid).status_code, 400
            )

    def test_browser_login_binding_and_no_application_token_in_redirect(self):
        from app.mcp_auth import finish_login
        from starlette.requests import Request

        client = self.register()
        response = self.begin(client)
        consent_url = response.headers["location"]
        pending_id = parse_qs(urlsplit(consent_url).query)["request_id"][0]
        login_redirect = self.client.get(
            "/oauth/login/google", params={"request_id": pending_id}
        )
        self.assertEqual(
            urlsplit(login_redirect.headers["location"]).hostname, "accounts.google.com"
        )
        cookie = self.client.cookies.get("yabus_mcp_browser")
        result = SimpleNamespace(redirect_uri=consent_url, login_result=self.login)
        request = Request(
            {
                "type": "http",
                "scheme": "https",
                "server": ("bus.example.test", 443),
                "path": "/api/v1/auth/google-callback",
                "headers": [(b"cookie", ("yabus_mcp_browser=" + cookie).encode())],
            }
        )
        completed = finish_login(self.provider, request, result)
        self.assertNotIn(self.login.token, completed.headers["location"])
        self.assertIn("HttpOnly", completed.headers["set-cookie"])
        self.assertNotIn(self.login.token, completed.headers["set-cookie"])
        from app.auth_service import authenticate_token

        self.assertIsNone(authenticate_token(self.settings, self.login.token))

    def test_wrong_client_cannot_redeem_refresh_or_revoke_another_grant(self):
        owner, other = self.register(), self.register()
        query, _, _ = self.consent(owner)
        code = query["code"][0]
        self.assertEqual(self.exchange(other, code).status_code, 400)
        tokens = self.exchange(owner, code).json()
        response = self.client.post(
            "/token",
            data={
                "client_id": other["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.client.post(
            "/revoke",
            data={"client_id": other["client_id"], "token": tokens["access_token"]},
        )
        self.assertEqual(
            self.rpc("/mcp", "tools/list", token=tokens["access_token"]).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/token",
                data={
                    "client_id": owner["client_id"],
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "scope": "favorites:write",
                },
            ).status_code,
            400,
        )

    def test_expiry_and_resource_binding_survive_reconfiguration(self):
        from dataclasses import replace

        client = self.register()
        query, _, _ = self.consent(client)
        tokens = self.exchange(client, query["code"][0]).json()
        other_provider = type(self.provider)(
            replace(self.settings, auth_public_base_url="https://another.example")
        )
        self.assertIsNone(
            asyncio.run(other_provider.load_access_token(tokens["access_token"]))
        )
        with get_connection(self.settings.app_db_path) as db:
            db.execute("UPDATE mcp_grants SET expires_at=0")
            db.commit()
        self.assertEqual(
            self.rpc("/mcp", "tools/list", token=tokens["access_token"]).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/token",
                data={
                    "client_id": client["client_id"],
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                },
            ).status_code,
            400,
        )

    def test_connection_management_is_account_scoped_and_requires_csrf(self):
        client = self.register(client_name="<script>bad</script>")
        query, _, _ = self.consent(client)
        tokens = self.exchange(client, query["code"][0]).json()
        grant = self.provider.account_grants(self.login.account_id)[0]
        self.provider.revoke_grant(grant["id"], self.other.account_id)
        self.assertEqual(
            self.rpc("/mcp", "tools/list", token=tokens["access_token"]).status_code,
            200,
        )
        page = self.client.get("/oauth/connections")
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", page.text)
        self.assertNotIn("<script>bad</script>", page.text)
        self.assertEqual(
            self.client.post(
                "/oauth/connections",
                data={"grant_id": grant["id"]},
                headers={"Origin": self.provider.issuer},
            ).status_code,
            403,
        )
        csrf = re.search("name='csrf' value='([^']+)'", page.text)[1]
        self.assertEqual(
            self.client.post(
                "/oauth/connections",
                data={"grant_id": grant["id"], "csrf": csrf},
                headers={"Origin": self.provider.issuer},
            ).status_code,
            303,
        )
        self.assertEqual(
            self.rpc("/mcp", "tools/list", token=tokens["access_token"]).status_code,
            401,
        )

    def test_favorite_arrivals_preserve_source_time_and_map_intercity_provider(self):
        from unittest.mock import patch

        payload = {
            "groups": {
                "Trip": [
                    {
                        "type": "boarding",
                        "provider": "tpe",
                        "routeKey": 1,
                        "pathId": 0,
                        "stopId": 12,
                        "routeId": "TPE307",
                        "routeName": "307",
                        "stopName": "Stop",
                        "rawStopId": "S1",
                    },
                    {
                        "type": "station",
                        "provider": "inter",
                        "stationId": "station-1",
                        "stationName": "Terminal",
                    },
                ]
            },
            "groupKinds": {"Trip": "mixed"},
        }
        upsert_sync_document(
            self.settings,
            account_id=self.login.account_id,
            namespace="favorites",
            schema_version=2,
            payload=payload,
            client_modified_at="2026-11-06T00:01:00Z",
            conflict_policy="client_wins",
        )
        client = self.register()
        query, _, _ = self.consent(client)
        token = self.exchange(client, query["code"][0]).json()["access_token"]
        with patch(
            "app.mcp_server.routes.get_station_passby",
            return_value={"station_id": "station-1"},
        ) as station:
            result = self.rpc(
                "/mcp",
                "tools/call",
                {"name": "get_favorite_arrivals", "arguments": {}},
                token,
            )
        data = result.json()["result"]["structuredContent"]
        self.assertEqual(data["items"][0]["arrivals"]["data"]["updated_at"], 123)
        self.assertEqual(
            data["items"][0]["arrivals"]["data"]["paths"][0]["stops"][0]["sec"], 180
        )
        self.assertEqual(station.call_args.kwargs["city"], "INT")

    def test_public_refresh_rotation_detects_reuse(self):
        client = self.register()
        query, _, _ = self.consent(client)
        tokens = self.exchange(client, query["code"][0]).json()
        data = {
            "client_id": client["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        }
        fresh = self.client.post("/token", data=data).json()
        self.assertNotEqual(fresh["refresh_token"], tokens["refresh_token"])
        self.assertEqual(self.client.post("/token", data=data).status_code, 400)
        self.assertEqual(
            self.rpc("/mcp", "tools/list", token=fresh["access_token"]).status_code, 401
        )

    def test_confidential_client_supports_persistent_linking(self):
        client = self.register("client_secret_post")
        query, _, _ = self.consent(client)
        tokens = self.exchange(
            client, query["code"][0], client_secret=client["client_secret"]
        ).json()
        response = self.client.post(
            "/token",
            data={
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["refresh_token"], tokens["refresh_token"])

    def test_access_logs_remove_authorization_query_values(self):
        import logging
        from app.logging_utils import OAuthAccessLogFilter

        record = logging.LogRecord(
            "uvicorn.access",
            20,
            "",
            0,
            '%s - "%s %s HTTP/%s" %d',
            (
                "client",
                "GET",
                "/api/v1/auth/google-callback?code=synthetic&state=test",
                "1.1",
                302,
            ),
            None,
        )
        OAuthAccessLogFilter().filter(record)
        self.assertNotIn("synthetic", record.getMessage())
        self.assertNotIn("?", record.getMessage())


if __name__ == "__main__":
    unittest.main()
