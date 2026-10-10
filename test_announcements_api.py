from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app.api import announcements as announcements_api
from app.api.announcements import (
    AnnouncementCreateRequest,
    AnnouncementUpdateRequest,
    ReactionToggleRequest,
    create_announcement,
    list_announcements,
    toggle_announcement_reaction,
    update_announcement,
)
from app.auth_service import AuthPrincipal, OAuthIdentity, _upsert_login
from app.config import Settings
from app.db import init_app_db, init_db
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
    def __init__(self, settings: Settings, *, user_agent: str) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        self.headers = {"user-agent": user_agent}
        self.state = SimpleNamespace()


class AnnouncementsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        init_app_db(self.db_path.parent / "app.db")
        self.settings = _settings(self.db_path)
        self._original_get_request_principal = announcements_api.get_request_principal
        self._original_send_announcement_push = announcements_api.send_announcement_push
        reset_rate_limit_state()

    def tearDown(self) -> None:
        announcements_api.get_request_principal = self._original_get_request_principal
        announcements_api.send_announcement_push = self._original_send_announcement_push
        self.temp_dir.cleanup()
        reset_rate_limit_state()

    def _create_account(self, provider_user_id: str) -> int:
        login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google",
                provider_user_id=provider_user_id,
                email=f"{provider_user_id}@example.test",
                display_name=provider_user_id,
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        return login.account_id

    def _request(self, *, user_agent: str = "YABus/1.3.1-abc123 (android)") -> _FakeRequest:
        return _FakeRequest(self.settings, user_agent=user_agent)

    def _set_principal(self, role: str | None, account_id: int = 1) -> None:
        if role is None:
            announcements_api.get_request_principal = lambda request: None
            return
        announcements_api.get_request_principal = lambda request: AuthPrincipal(
            account_id=account_id,
            device_id=2,
            token_id=3,
            role=role,
        )

    def test_main_app_registers_announcement_routes(self) -> None:
        self.assertEqual(str(app.url_path_for("announcements_page")), "/admin/announcements")
        self.assertEqual(str(app.url_path_for("list_announcements")), "/api/v1/announcements")
        self.assertEqual(str(app.url_path_for("update_announcement", announcement_id=1)),
                         "/api/v1/announcements/1")

    def test_create_and_patch_require_moderator_role(self) -> None:
        request = self._request()
        payload = AnnouncementCreateRequest(title="Announcement", content="Hello")

        self._set_principal("user")
        with self.assertRaises(HTTPException) as denied_create:
            create_announcement(request, payload)
        self.assertEqual(denied_create.exception.status_code, 403)

        self._set_principal("mod")
        created = create_announcement(request, payload)
        self.assertEqual(created["author"], "mod")

        self._set_principal("user")
        with self.assertRaises(HTTPException) as denied_patch:
            update_announcement(
                created["id"],
                request,
                AnnouncementUpdateRequest(title="Updated"),
            )
        self.assertEqual(denied_patch.exception.status_code, 403)

    def test_list_announcements_filters_expired_and_targets(self) -> None:
        request = self._request(user_agent="YABus/1.3.1-abc123 (android)")
        self._set_principal("admin")

        create_announcement(
            request,
            AnnouncementCreateRequest(
                id="visible",
                title="Visible",
                content="Hello",
                targets={"platforms": ["android"], "version_constraint": ">=1.3.0"},
            ),
        )
        create_announcement(
            request,
            AnnouncementCreateRequest(
                id="expired",
                title="Expired",
                content="Bye",
                created_at=1,
                expire_at=2,
            ),
        )
        create_announcement(
            request,
            AnnouncementCreateRequest(
                id="web-only",
                title="Web",
                content="Web only",
                targets={"platforms": ["web"]},
            ),
        )
        create_announcement(
            request,
            AnnouncementCreateRequest(
                id="future-version",
                title="Future",
                content="Future",
                targets={"platforms": ["android"], "version_constraint": ">=9.0.0"},
            ),
        )

        announcements = list_announcements(request)

        self.assertEqual([item["id"] for item in announcements], ["visible"])

    def test_patch_can_clear_optional_fields(self) -> None:
        request = self._request()
        self._set_principal("admin")
        created = create_announcement(
            request,
            AnnouncementCreateRequest(
                id="clearable",
                title="Clearable",
                content="Body",
                author="Author",
                expire_at=9999999999,
                sound_url="https://example.com/notify.mp3",
                targets={"platforms": ["android"], "version_constraint": ">=1.0.0"},
                embed={"type": "youtube", "url": "https://youtube.com/embed/demo"},
                actions=[
                    {
                        "type": "deeplink",
                        "label": "Open",
                        "url": "https://busapp.avianjay.sbs/terms-of-service",
                    }
                ],
            ),
        )

        updated = update_announcement(
            created["id"],
            request,
            AnnouncementUpdateRequest(
                author=None,
                expire_at=None,
                sound_url=None,
                targets=None,
                embed=None,
                actions=None,
            ),
        )

        self.assertIsNone(updated["author"])
        self.assertIsNone(updated["expire_at"])
        self.assertIsNone(updated["sound_url"])
        self.assertIsNone(updated["targets"])
        self.assertIsNone(updated["embed"])
        self.assertIsNone(updated["actions"])

    def test_behavior_supports_none_for_red_dot_and_popup(self) -> None:
        request = self._request()
        self._set_principal("admin")

        created = create_announcement(
            request,
            AnnouncementCreateRequest(
                id="silent",
                title="Silent",
                content="No badge, no popup",
                behavior={"red_dot": "none", "popup": "none"},
            ),
        )

        self.assertEqual(created["behavior"], {"red_dot": "none", "popup": "none"})

        updated = update_announcement(
            created["id"],
            request,
            AnnouncementUpdateRequest(
                behavior={"red_dot": "forever", "popup": "none"},
            ),
        )

        self.assertEqual(updated["behavior"], {"red_dot": "forever", "popup": "none"})

    def test_create_with_push_flag_calls_sender_once(self) -> None:
        request = self._request()
        self._set_principal("admin")
        calls: list[dict[str, object]] = []
        announcements_api.send_announcement_push = lambda settings, announcement: (
            calls.append({"settings": settings, "announcement": dict(announcement)}) or {
                "attempted": True,
                "sent": 2,
                "failed": 0,
                "invalidated": 0,
                "skipped": False,
            }
        )

        created = create_announcement(
            request,
            AnnouncementCreateRequest(
                id="push-test",
                title="Push",
                content="Send it",
                send_push_notification=True,
            ),
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["settings"], self.settings)
        self.assertEqual(calls[0]["announcement"]["id"], "push-test")
        self.assertEqual(created["push_notification"]["sent"], 2)

    def _seed_announcement(self, announcement_id: str) -> str:
        request = self._request()
        self._set_principal("admin", account_id=1)
        create_announcement(
            request,
            AnnouncementCreateRequest(
                id=announcement_id,
                title="Reactable",
                content="React to me",
            ),
        )
        return announcement_id

    def test_toggle_reaction_requires_authentication(self) -> None:
        self._seed_announcement("react-auth")
        request = self._request()
        self._set_principal(None)

        with self.assertRaises(HTTPException) as denied:
            toggle_announcement_reaction(
                "react-auth", request, ReactionToggleRequest(emoji="👍")
            )
        self.assertEqual(denied.exception.status_code, 401)

    def test_toggle_reaction_adds_then_removes(self) -> None:
        self._seed_announcement("react-toggle")
        request = self._request()
        self._set_principal("user", account_id=self._create_account("react-user"))

        added = toggle_announcement_reaction(
            "react-toggle", request, ReactionToggleRequest(emoji="👍")
        )
        self.assertEqual(added["reactions"], [{"emoji": "👍", "count": 1}])
        self.assertEqual(added["my_reactions"], ["👍"])

        removed = toggle_announcement_reaction(
            "react-toggle", request, ReactionToggleRequest(emoji="👍")
        )
        self.assertEqual(removed["reactions"], [])
        self.assertEqual(removed["my_reactions"], [])

    def test_toggle_reaction_two_users_same_emoji_counts_two(self) -> None:
        self._seed_announcement("react-shared")
        request = self._request()

        self._set_principal("user", account_id=self._create_account("react-user-a"))
        toggle_announcement_reaction(
            "react-shared", request, ReactionToggleRequest(emoji="❤️")
        )
        self._set_principal("user", account_id=self._create_account("react-user-b"))
        result = toggle_announcement_reaction(
            "react-shared", request, ReactionToggleRequest(emoji="❤️")
        )

        self.assertEqual(result["reactions"], [{"emoji": "❤️", "count": 2}])
        self.assertEqual(result["my_reactions"], ["❤️"])

    def test_toggle_reaction_multiple_distinct_emojis_persist(self) -> None:
        self._seed_announcement("react-multi")
        request = self._request()
        self._set_principal("user", account_id=self._create_account("react-multi-user"))

        toggle_announcement_reaction(
            "react-multi", request, ReactionToggleRequest(emoji="👍")
        )
        result = toggle_announcement_reaction(
            "react-multi", request, ReactionToggleRequest(emoji="🎉")
        )

        self.assertEqual(
            {item["emoji"] for item in result["reactions"]}, {"👍", "🎉"}
        )
        self.assertEqual(sorted(result["my_reactions"]), sorted(["👍", "🎉"]))

    def test_list_includes_reactions_and_scopes_my_reactions(self) -> None:
        self._seed_announcement("react-list")
        request = self._request()
        reacting_account = self._create_account("react-list-user")
        other_account = self._create_account("react-list-other")
        self._set_principal("user", account_id=reacting_account)
        toggle_announcement_reaction(
            "react-list", request, ReactionToggleRequest(emoji="👍")
        )

        # The reacting user sees their own reaction.
        mine = list_announcements(request)[0]
        self.assertEqual(mine["reactions"], [{"emoji": "👍", "count": 1}])
        self.assertEqual(mine["my_reactions"], ["👍"])

        # A different authenticated user sees the count but not my_reactions.
        self._set_principal("user", account_id=other_account)
        other = list_announcements(request)[0]
        self.assertEqual(other["reactions"], [{"emoji": "👍", "count": 1}])
        self.assertEqual(other["my_reactions"], [])

        # An anonymous caller sees the count but no my_reactions.
        self._set_principal(None)
        anon = list_announcements(request)[0]
        self.assertEqual(anon["reactions"], [{"emoji": "👍", "count": 1}])
        self.assertEqual(anon["my_reactions"], [])

    def test_toggle_reaction_missing_announcement_returns_404(self) -> None:
        request = self._request()
        self._set_principal("user", account_id=self._create_account("react-missing-user"))

        with self.assertRaises(HTTPException) as missing:
            toggle_announcement_reaction(
                "does-not-exist", request, ReactionToggleRequest(emoji="👍")
            )
        self.assertEqual(missing.exception.status_code, 404)

    def test_reaction_toggle_request_rejects_invalid_emoji(self) -> None:
        for invalid in ("", "abc", "x" * 40):
            with self.assertRaises(ValidationError):
                ReactionToggleRequest(emoji=invalid)


if __name__ == "__main__":
    unittest.main()
