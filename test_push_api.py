from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.api.push import (
    PushTokenRegisterRequest,
    public_push_config,
    register_push_token,
)
from app.config import Settings
from app.db import get_connection, init_app_db, init_db
from app.main import app


def _settings(db_path: Path, *, vapid_key: str = "") -> Settings:
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
        fcm_web_vapid_key=vapid_key,
    )


class _FakeRequest:
    def __init__(self, settings: Settings, *, user_agent: str) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        self.headers = {"user-agent": user_agent}
        self.state = SimpleNamespace()


class PushApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.app_db_path = self.db_path.parent / "app.db"
        init_app_db(self.app_db_path)
        self.settings = _settings(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _request(self, *, user_agent: str = "YABus/1.0.0-abcdef (web)") -> _FakeRequest:
        return _FakeRequest(self.settings, user_agent=user_agent)

    def test_main_app_registers_push_routes(self) -> None:
        self.assertEqual(str(app.url_path_for("public_push_config")), "/api/v1/push/public-config")
        self.assertEqual(str(app.url_path_for("register_push_token")), "/api/v1/push/fcm-token")

    def test_register_push_token_upserts_existing_token(self) -> None:
        request = self._request()

        result = register_push_token(
            request,
            PushTokenRegisterRequest(token="abc", platform="web"),
        )
        self.assertEqual(result, {"ok": True})

        register_push_token(
            request,
            PushTokenRegisterRequest(token="abc", platform="android"),
        )

        with get_connection(self.app_db_path) as connection:
            rows = connection.execute(
                """
                SELECT token, platform, user_agent
                FROM announcement_push_tokens
                ORDER BY token
                """
            ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token"], "abc")
        self.assertEqual(rows[0]["platform"], "android")
        self.assertEqual(rows[0]["user_agent"], "YABus/1.0.0-abcdef (web)")

    def test_public_push_config_reports_web_enabled_only_with_vapid_key(self) -> None:
        disabled = public_push_config(self._request())
        self.assertFalse(disabled["web_enabled"])
        self.assertEqual(disabled["web"]["projectId"], "yabus-111c1")

        self.settings = _settings(self.db_path, vapid_key="test-vapid")
        enabled = public_push_config(self._request())
        self.assertTrue(enabled["web_enabled"])
        self.assertEqual(enabled["web"]["vapidKey"], "test-vapid")


if __name__ == "__main__":
    unittest.main()
