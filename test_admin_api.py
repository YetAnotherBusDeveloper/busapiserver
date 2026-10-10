from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.api import admin as admin_api
from app.api import analytics as analytics_api
from app.auth_service import AuthPrincipal, OAuthIdentity, _upsert_login, authenticate_token
from app.config import Settings
from app.db import get_connection, init_app_db, init_db
from app.main import app
from app.static_sync_control import StaticSyncCoordinator


def _settings(db_path: Path) -> Settings:
    return Settings(
        project_dir=db_path.parent,
        db_path=db_path,
        download_db_path=db_path.parent / "downloads" / "bus.db",
        tdx_client_id=None,
        tdx_client_secret=None,
        tdx_base_url="https://example.test",
        tdx_token_url="https://example.test/token",
        tdx_cities=(),
        tdx_request_timeout=30,
        tdx_token_refresh_skew=300,
        tdx_retry_attempts=1,
        tdx_retry_backoff=1.0,
        tdx_min_request_interval=0.0,
        realtime_cache_ttl=5,
        realtime_track_ttl=30,
        cors_origins=(),
        auth_public_base_url="https://bus.example.test",
        auth_state_ttl_seconds=600,
        auth_snowflake_node_id=0,
        discord_oauth_client_id="discord-client",
        discord_oauth_client_secret="discord-secret",
        google_oauth_client_id="google-client",
        google_oauth_client_secret="google-secret",
        google_native_oauth_client_ids=("android-client", "ios-client"),
    )


class _FakeRequest:
    def __init__(self, settings: Settings, **state) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings, **state))
        self.headers = {"user-agent": "YABus/1.3.1-abc123 (windows)"}
        self.state = SimpleNamespace()


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.app_db_path = self.db_path.parent / "app.db"
        init_app_db(self.app_db_path)
        self.settings = _settings(self.db_path)
        self._original_admin_get_request_principal = admin_api.get_request_principal
        self._original_analytics_get_request_principal = analytics_api.get_request_principal

    def tearDown(self) -> None:
        admin_api.get_request_principal = self._original_admin_get_request_principal
        analytics_api.get_request_principal = self._original_analytics_get_request_principal
        self.temp_dir.cleanup()

    def _request(self) -> _FakeRequest:
        return _FakeRequest(self.settings)

    def _set_principal(self, role: str | None) -> None:
        if role is None:
            admin_api.get_request_principal = lambda request: None
            analytics_api.get_request_principal = lambda request: None
            return

        principal = AuthPrincipal(
            account_id=1,
            device_id=2,
            token_id=3,
            role=role,
        )
        admin_api.get_request_principal = lambda request: principal
        analytics_api.get_request_principal = lambda request: principal

    def test_main_app_registers_admin_routes(self) -> None:
        # Public reverse routing works with both flat and nested router storage.
        for name, path in (
            ("analytics_dashboard", "/admin/analytics"),
            ("announcements_page", "/admin/announcements"),
            ("user_manage_page", "/admin/user_manage"),
            ("get_analytics", "/api/v1/admin/analytics"),
            ("list_users", "/api/v1/admin/users"),
            ("trigger_static_sync", "/api/v1/admin/static-sync"),
        ):
            self.assertEqual(str(app.url_path_for(name)), path)

    def test_admin_pages_require_login_or_admin_role(self) -> None:
        request = self._request()

        self._set_principal(None)
        user_page = admin_api.user_manage_page(request)
        analytics_page = analytics_api.analytics_dashboard(request)
        self.assertIsInstance(user_page, RedirectResponse)
        self.assertIsInstance(analytics_page, RedirectResponse)

        self._set_principal("user")
        with self.assertRaises(HTTPException) as user_manage_denied:
            admin_api.list_users(request)
        self.assertEqual(user_manage_denied.exception.status_code, 403)

        with self.assertRaises(HTTPException) as analytics_denied:
            analytics_api.get_analytics(request)
        self.assertEqual(analytics_denied.exception.status_code, 403)

    def test_admin_user_api_updates_roles_and_revokes_sessions(self) -> None:
        admin_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="admin-user",
                email="admin@example.test",
                display_name="Admin User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        member_login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="discord",
                provider_user_id="member-user",
                email="member@example.test",
                display_name="Member User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        with get_connection(self.app_db_path) as connection:
            connection.execute(
                "UPDATE accounts SET role = 'admin' WHERE id = ?",
                (admin_login.account_id,),
            )
            connection.commit()

        self._set_principal("admin")
        request = self._request()

        users_payload = admin_api.list_users(request)
        self.assertEqual(users_payload["summary"]["admin_count"], 1)
        self.assertEqual(len(users_payload["accounts"]), 2)

        updated = admin_api.patch_user_role(
            str(member_login.account_id),
            request,
            admin_api.AccountRoleUpdateRequest(role="mod"),
        )
        self.assertEqual(updated["role"], "mod")

        revoked = admin_api.logout_user_devices(str(member_login.account_id), request)
        self.assertTrue(revoked["ok"])
        self.assertEqual(revoked["revoked_sessions"], 1)
        self.assertIsNone(authenticate_token(self.settings, member_login.token))


class AdminStaticSyncApiTests(unittest.TestCase):
    """The only way to sync on a deployment with no shell access."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        init_app_db(self.db_path.parent / "app.db")
        self.settings = _settings(self.db_path)
        self.coordinator = StaticSyncCoordinator()
        self._original = admin_api.get_request_principal

    def tearDown(self) -> None:
        admin_api.get_request_principal = self._original
        self.temp_dir.cleanup()

    def _request(self, *, with_coordinator: bool = True) -> _FakeRequest:
        if with_coordinator:
            return _FakeRequest(self.settings, static_sync_coordinator=self.coordinator)
        return _FakeRequest(self.settings)

    def _set_principal(self, role: str | None) -> None:
        if role is None:
            admin_api.get_request_principal = lambda request: None
            return
        principal = AuthPrincipal(account_id=1, device_id=2, token_id=3, role=role)
        admin_api.get_request_principal = lambda request: principal

    def test_requires_authentication(self) -> None:
        self._set_principal(None)
        with self.assertRaises(HTTPException) as ctx:
            admin_api.trigger_static_sync(self._request(), None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_requires_admin_role(self) -> None:
        self._set_principal("user")
        with self.assertRaises(HTTPException) as ctx:
            admin_api.trigger_static_sync(self._request(), None)
        self.assertEqual(ctx.exception.status_code, 403)

        with self.assertRaises(HTTPException) as status_ctx:
            admin_api.get_static_sync_status(self._request())
        self.assertEqual(status_ctx.exception.status_code, 403)

    def test_admin_can_queue_a_sync(self) -> None:
        self._set_principal("admin")

        result = admin_api.trigger_static_sync(self._request(), None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"]["state"], "queued")
        self.assertIsNone(result["status"]["cities"])
        self.assertTrue(self.coordinator.wait_for_trigger(timeout=0))

    def test_admin_can_scope_the_sync_to_cities(self) -> None:
        self._set_principal("admin")

        result = admin_api.trigger_static_sync(
            self._request(),
            admin_api.StaticSyncRequest(cities=["Hsinchu"]),
        )

        self.assertEqual(result["status"]["cities"], ["Hsinchu"])

    def test_unknown_city_is_rejected_rather_than_silently_ignored(self) -> None:
        with self.assertRaises(ValidationError):
            admin_api.StaticSyncRequest(cities=["Atlantis"])

    def test_second_trigger_conflicts_while_one_is_in_flight(self) -> None:
        self._set_principal("admin")
        admin_api.trigger_static_sync(self._request(), None)

        with self.assertRaises(HTTPException) as ctx:
            admin_api.trigger_static_sync(self._request(), None)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_status_reports_the_outcome(self) -> None:
        self._set_principal("admin")
        admin_api.trigger_static_sync(self._request(), None)
        self.coordinator.run_claimed(lambda *, cities, force: None)

        status = admin_api.get_static_sync_status(self._request())

        self.assertEqual(status["state"], "idle")
        self.assertIsNone(status["last_error"])
        self.assertIsNotNone(status["finished_at"])

    def test_missing_scheduler_reports_unavailable(self) -> None:
        self._set_principal("admin")
        with self.assertRaises(HTTPException) as ctx:
            admin_api.trigger_static_sync(self._request(with_coordinator=False), None)
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
