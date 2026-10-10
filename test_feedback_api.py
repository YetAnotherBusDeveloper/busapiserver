from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from app.api import feedback as feedback_api
from app.auth_service import AuthPrincipal, OAuthIdentity, _upsert_login
from app.config import Settings
from app.db import get_connection, init_app_db, init_db
from app.main import app
from app.rate_limit import reset_rate_limit_state


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
    def __init__(self, settings: Settings, *, user_agent: str = "YABus/1.3.1-abc123 (android)") -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        self.headers = {"user-agent": user_agent}
        self.state = SimpleNamespace()


class FeedbackApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.app_db_path = self.db_path.parent / "app.db"
        init_app_db(self.app_db_path)
        self.settings = _settings(self.db_path)
        self._original_get_request_principal = feedback_api.get_request_principal
        reset_rate_limit_state()

    def tearDown(self) -> None:
        feedback_api.get_request_principal = self._original_get_request_principal
        self.temp_dir.cleanup()
        reset_rate_limit_state()

    def _request(self, *, user_agent: str = "YABus/1.3.1-abc123 (android)") -> _FakeRequest:
        return _FakeRequest(self.settings, user_agent=user_agent)

    def _set_principal(self, role: str | None, *, account_id: int = 1) -> None:
        if role is None:
            feedback_api.get_request_principal = lambda request: None
            return
        feedback_api.get_request_principal = lambda request: AuthPrincipal(
            account_id=account_id,
            device_id=2,
            token_id=3,
            role=role,
        )

    def test_main_app_registers_feedback_routes(self) -> None:
        self.assertEqual(str(app.url_path_for("create_feedback")), "/api/v1/feedback")
        self.assertEqual(str(app.url_path_for("list_feedbacks")), "/api/v1/admin/feedbacks")
        self.assertEqual(str(app.url_path_for("feedbacks_page")), "/admin/feedbacks")

    def test_feedback_create_requires_login(self) -> None:
        self._set_principal(None)

        with self.assertRaises(HTTPException) as context:
            feedback_api.create_feedback(
                self._request(),
                feedback_api.FeedbackCreateRequest(title="Oops", content="No login"),
            )

        self.assertEqual(context.exception.status_code, 401)

    def test_feedback_create_is_limited_per_user_and_persists_client_info(self) -> None:
        login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id="feedback-user",
                email="feedback@example.test",
                display_name="Feedback User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        self._set_principal("user", account_id=login.account_id)
        request = self._request(user_agent="YABus/2.0.0-deadbee (ios)")

        created = feedback_api.create_feedback(
            request,
            feedback_api.FeedbackCreateRequest(title="Need this", content="Please add this feature."),
        )

        self.assertTrue(created["ok"])
        with get_connection(self.app_db_path) as connection:
            row = connection.execute(
                """
                SELECT account_id, title, content, client_family, platform_name, app_version
                FROM feedbacks
                """,
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row["account_id"]), login.account_id)
        self.assertEqual(row["title"], "Need this")
        self.assertEqual(row["content"], "Please add this feature.")
        self.assertEqual(row["client_family"], "app")
        self.assertEqual(row["platform_name"], "ios")
        self.assertEqual(row["app_version"], "2.0.0")

        with self.assertRaises(HTTPException) as limited:
            feedback_api.create_feedback(
                request,
                feedback_api.FeedbackCreateRequest(title="Again", content="Second post"),
            )
        self.assertEqual(limited.exception.status_code, 429)
        self.assertEqual(
            limited.exception.detail,
            "Feedback submissions are limited to 1 per minute.",
        )

    def test_feedback_admin_routes_require_admin(self) -> None:
        request = self._request()

        self._set_principal(None)
        page = feedback_api.feedbacks_page(request)
        self.assertIsInstance(page, RedirectResponse)

        self._set_principal("user")
        with self.assertRaises(HTTPException) as denied:
            feedback_api.list_feedbacks(request, limit=200)
        self.assertEqual(denied.exception.status_code, 403)

    def test_feedback_admin_list_returns_identity_context(self) -> None:
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

        self._set_principal("user", account_id=member_login.account_id)
        feedback_api.create_feedback(
            self._request(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"),
            feedback_api.FeedbackCreateRequest(title="Web report", content="Submitted from browser."),
        )

        self._set_principal("admin", account_id=admin_login.account_id)
        payload = feedback_api.list_feedbacks(self._request(), limit=200)

        self.assertEqual(payload["summary"]["total_count"], 1)
        self.assertEqual(len(payload["feedbacks"]), 1)
        feedback = payload["feedbacks"][0]
        self.assertEqual(feedback["account_id"], str(member_login.account_id))
        self.assertEqual(feedback["account_label"], "Member User")
        self.assertEqual(feedback["account_email"], "member@example.test")
        self.assertEqual(feedback["browser_name"], "Chrome")
        self.assertEqual(feedback["client_family"], "web")

    def test_feedback_page_serves_html_for_admin(self) -> None:
        self._set_principal("admin")

        response = feedback_api.feedbacks_page(self._request())

        self.assertIsInstance(response, FileResponse)


if __name__ == "__main__":
    unittest.main()
