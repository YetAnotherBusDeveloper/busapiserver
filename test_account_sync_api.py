from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException, Response

from app.api import account_sync as account_sync_api
from app.auth_service import AuthPrincipal, OAuthIdentity, _upsert_login
from app.config import Settings
from app.db import get_connection, init_app_db, init_db
from app.main import app


def _settings(
    db_path: Path,
    *,
    account_sync_max_payload_bytes: int = 512 * 1024,
) -> Settings:
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
        account_sync_max_payload_bytes=account_sync_max_payload_bytes,
    )


class _FakeRequest:
    def __init__(
        self,
        settings: Settings,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        self.headers = headers or {}
        self.state = SimpleNamespace()
        self._body = body

    async def body(self) -> bytes:
        return self._body


class AccountSyncApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        self.app_db_path = self.db_path.parent / "app.db"
        init_db(self.db_path)
        init_app_db(self.app_db_path)
        self.settings = _settings(self.db_path)
        self._original_get_request_principal = account_sync_api.get_request_principal

    def tearDown(self) -> None:
        account_sync_api.get_request_principal = self._original_get_request_principal
        self.temp_dir.cleanup()

    def _set_principal(self, account_id: int | None) -> None:
        if account_id is None:
            account_sync_api.get_request_principal = lambda request: None
            return
        account_sync_api.get_request_principal = lambda request: AuthPrincipal(
            account_id=account_id,
            device_id=2,
            token_id=3,
            role="user",
        )

    def _create_account(self, *, provider: str = "google", user_id: str | None = None) -> int:
        login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider=provider,
                provider_user_id=user_id or str(uuid4()),
                email="sync@example.test",
                display_name="Sync User",
                avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        return login.account_id

    def _put_document(
        self,
        namespace: str,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
        settings: Settings | None = None,
    ) -> tuple[Response, dict[str, object]]:
        response = Response()
        request = _FakeRequest(
            settings or self.settings,
            body=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )
        result = asyncio.run(
            account_sync_api.put_account_sync_document(
                request,
                response,
                namespace,  # type: ignore[arg-type]
            )
        )
        return response, result

    def test_main_app_registers_account_sync_routes(self) -> None:
        self.assertEqual(str(app.url_path_for("account_sync_status")), "/api/v1/account/sync")
        self.assertEqual(str(app.url_path_for("account_sync_document", namespace="favorites")),
                         "/api/v1/account/sync/favorites")

    def test_sync_endpoints_require_login(self) -> None:
        self._set_principal(None)
        request = _FakeRequest(self.settings)

        with self.assertRaises(HTTPException) as summary_error:
            account_sync_api.account_sync_status(request, Response())
        self.assertEqual(summary_error.exception.status_code, 401)

        with self.assertRaises(HTTPException) as put_error:
            asyncio.run(
                account_sync_api.put_account_sync_document(
                    request,
                    Response(),
                    "favorites",  # type: ignore[arg-type]
                )
            )
        self.assertEqual(put_error.exception.status_code, 401)

    def test_favorites_sync_creates_document_preserves_empty_groups_and_sets_headers(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)

        response, result = self._put_document(
            "favorites",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T01:02:03Z",
                "payload": {
                    "groups": {
                        " 通勤 ": [
                            {
                                "provider": "TPE",
                                "routeKey": 123,
                                "pathId": 0,
                                "stopId": 456,
                                "routeName": "紅 12 ",
                                "stopName": " 市政府 ",
                                "destinationStopId": 789,
                            }
                        ],
                        "空分類": [],
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(result["status"], "created")
        document = result["document"]
        assert isinstance(document, dict)
        self.assertTrue(document["has_data"])
        self.assertEqual(document["revision"], 1)
        self.assertEqual(
            document["payload"]["groups"],  # type: ignore[index]
            {
                "通勤": [
                    {
                        "provider": "tpe",
                        "routeKey": 123,
                        "pathId": 0,
                        "stopId": 456,
                        "routeName": "紅 12",
                        "stopName": "市政府",
                        "destinationPathId": 0,
                        "destinationStopId": 789,
                    }
                ],
                "空分類": [],
            },
        )
        self.assertIn("ETag", response.headers)
        self.assertIn("Last-Modified", response.headers)
        self.assertEqual(response.headers["X-YABUS-Sync-Revision"], "1")

        with get_connection(self.app_db_path) as connection:
            row = connection.execute(
                """
                SELECT namespace, schema_version, revision, payload_size_bytes
                FROM account_sync_documents
                WHERE account_id = ? AND namespace = 'favorites'
                """,
                (account_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["namespace"], "favorites")
        self.assertEqual(int(row["schema_version"]), 1)
        self.assertEqual(int(row["revision"]), 1)
        self.assertGreater(int(row["payload_size_bytes"]), 0)

    def test_account_sync_status_lists_documents(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        self._put_document(
            "preferences",
            {
                "schema_version": 3,
                "client_modified_at": "2026-05-21T02:00:00+08:00",
                "payload": {
                    "themeMode": "dark",
                    "seedColor": 4280391411,
                },
            },
        )

        response = Response()
        payload = account_sync_api.account_sync_status(
            _FakeRequest(self.settings),
            response,
        )

        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("server_time", payload)
        self.assertFalse(payload["documents"]["favorites"]["has_data"])
        self.assertTrue(payload["documents"]["preferences"]["has_data"])
        self.assertEqual(payload["documents"]["preferences"]["schema_version"], 3)

    def test_get_document_returns_304_when_etag_matches(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        put_response, put_result = self._put_document(
            "preferences",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T02:00:00Z",
                "payload": {"themeMode": "dark"},
            },
        )
        etag = put_response.headers["ETag"]
        self.assertTrue(etag)

        result = account_sync_api.account_sync_document(
            _FakeRequest(self.settings, headers={"if-none-match": etag}),
            Response(),
            "preferences",  # type: ignore[arg-type]
        )

        self.assertEqual(result.status_code, 304)  # type: ignore[union-attr]
        self.assertEqual(result.headers["ETag"], put_result["document"]["etag"])  # type: ignore[index]

    def test_favorites_validation_rejects_too_many_favorites(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        favorites = [
            {
                "provider": "tpe",
                "routeKey": index + 1,
                "pathId": 0,
                "stopId": index + 1000,
            }
            for index in range(26)
        ]

        with self.assertRaises(HTTPException) as context:
            self._put_document(
                "favorites",
                {
                    "schema_version": 1,
                    "client_modified_at": "2026-05-21T03:00:00Z",
                    "payload": {"groups": {"A": favorites}},
                },
            )

        self.assertEqual(context.exception.status_code, 422)
        self.assertIn("maximum of 25 items", context.exception.detail)

    def test_favorites_merge_preserves_new_empty_group(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        favorite = {
            "provider": "tpe",
            "routeKey": 123,
            "pathId": 0,
            "stopId": 456,
        }

        self._put_document(
            "favorites",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T03:00:00Z",
                "payload": {"groups": {"Existing group": [favorite]}},
            },
        )
        response, result = self._put_document(
            "favorites",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T03:05:00Z",
                "conflict_policy": "merge",
                "payload": {"groups": {"New group": []}},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(
            result["document"]["payload"]["groups"],  # type: ignore[index]
            {
                "Existing group": [favorite],
                "New group": [],
            },
        )

    def test_favorites_v2_accepts_all_item_types_and_preserves_empty_typed_group(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)

        _, result = self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:10:00Z",
                "payload": {
                    "groupKinds": {
                        "路線": "route",
                        "站牌": "station",
                        "乘車": "boarding",
                        "綜合": "mixed",
                    },
                    "groups": {
                        "路線": [
                            {
                                "type": "route",
                                "provider": "TPE",
                                "routeKey": 101,
                                "routeId": "TPE-101",
                                "routeName": " 紅 12 ",
                                "routeDescription": " 市政府－捷運站 ",
                            }
                        ],
                        "站牌": [
                            {
                                "type": "station",
                                "provider": "TPE",
                                "stationId": "TPE-STATION-1",
                                "stationName": " 市政府 ",
                            }
                        ],
                        "乘車": [
                            {
                                "type": "boarding",
                                "provider": "TPE",
                                "routeKey": 101,
                                "pathId": 0,
                                "stopId": 202,
                                "rawStopId": " TPE-STOP-202 ",
                            }
                        ],
                        "綜合": [],
                    },
                },
            },
        )

        document = result["document"]
        self.assertEqual(document["schema_version"], 2)  # type: ignore[index]
        payload = document["payload"]  # type: ignore[index]
        self.assertEqual(payload["groupKinds"]["綜合"], "mixed")
        self.assertEqual(payload["groups"]["路線"][0]["routeName"], "紅 12")
        self.assertEqual(payload["groups"]["站牌"][0]["stationName"], "市政府")
        self.assertEqual(payload["groups"]["乘車"][0]["rawStopId"], "TPE-STOP-202")

    def test_favorites_v2_rejects_group_mismatch_and_unrelated_fields(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        base_request = {
            "schema_version": 2,
            "client_modified_at": "2026-05-21T03:11:00Z",
            "payload": {
                "groupKinds": {"路線": "route"},
                "groups": {
                    "路線": [
                        {
                            "type": "station",
                            "provider": "tpe",
                            "stationId": "S1",
                            "stationName": "站牌",
                        }
                    ]
                },
            },
        }

        with self.assertRaises(HTTPException) as mismatch:
            self._put_document("favorites", base_request)
        self.assertEqual(mismatch.exception.status_code, 422)
        self.assertIn("cannot contain", mismatch.exception.detail)

        route_item = {
            "type": "route",
            "provider": "tpe",
            "routeKey": 1,
            "routeId": "R1",
            "routeName": "1",
            "stopId": 99,
        }
        base_request["payload"] = {
            "groupKinds": {"路線": "route"},
            "groups": {"路線": [route_item]},
        }
        with self.assertRaises(HTTPException) as unrelated:
            self._put_document("favorites", base_request)
        self.assertEqual(unrelated.exception.status_code, 422)
        self.assertIn("unsupported keys", unrelated.exception.detail)

    def test_favorites_v2_upgrade_preserves_existing_v1_boarding(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        legacy = {"provider": "tpe", "routeKey": 10, "pathId": 0, "stopId": 20}
        self._put_document(
            "favorites",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T03:12:00Z",
                "payload": {"groups": {"舊收藏": [legacy]}},
            },
        )

        _, result = self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:13:00Z",
                "base_revision": 1,
                "payload": {
                    "groupKinds": {"路線": "route"},
                    "groups": {
                        "路線": [
                            {
                                "type": "route",
                                "provider": "tpe",
                                "routeKey": 30,
                                "routeId": "R30",
                                "routeName": "30",
                            }
                        ]
                    },
                },
            },
        )

        document = result["document"]
        self.assertEqual(document["schema_version"], 2)  # type: ignore[index]
        payload = document["payload"]  # type: ignore[index]
        self.assertEqual(payload["groupKinds"]["舊收藏"], "boarding")
        self.assertEqual(payload["groups"]["舊收藏"][0], {"type": "boarding", **legacy})
        self.assertEqual(payload["groupKinds"]["路線"], "route")

    def test_matching_v1_write_only_replaces_boarding_projection_of_v2(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:14:00Z",
                "payload": {
                    "groupKinds": {"通勤": "mixed", "空路線": "route"},
                    "groups": {
                        "通勤": [
                            {
                                "type": "station",
                                "provider": "tpe",
                                "stationId": "S1",
                                "stationName": "站牌",
                            },
                            {
                                "type": "boarding",
                                "provider": "tpe",
                                "routeKey": 1,
                                "pathId": 0,
                                "stopId": 2,
                            },
                        ],
                        "空路線": [],
                    },
                },
            },
        )

        _, result = self._put_document(
            "favorites",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T03:15:00Z",
                "base_revision": 1,
                "payload": {"groups": {"通勤": []}},
            },
        )

        document = result["document"]
        self.assertEqual(document["schema_version"], 2)  # type: ignore[index]
        payload = document["payload"]  # type: ignore[index]
        self.assertEqual([item["type"] for item in payload["groups"]["通勤"]], ["station"])
        self.assertEqual(payload["groupKinds"]["通勤"], "mixed")
        self.assertEqual(payload["groupKinds"]["空路線"], "route")
        self.assertEqual(payload["groups"]["空路線"], [])

    def test_v1_client_wins_preserves_v2_items_and_coerces_typed_group_to_mixed(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:16:00Z",
                "payload": {
                    "groupKinds": {"站牌": "station"},
                    "groups": {
                        "站牌": [
                            {
                                "type": "station",
                                "provider": "tpe",
                                "stationId": "S1",
                                "stationName": "站牌",
                            }
                        ]
                    },
                },
            },
        )
        boarding = {"provider": "tpe", "routeKey": 3, "pathId": 0, "stopId": 4}

        _, result = self._put_document(
            "favorites",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T03:17:00Z",
                "base_revision": 999,
                "conflict_policy": "client_wins",
                "payload": {"groups": {"站牌": [boarding]}},
            },
        )

        payload = result["document"]["payload"]  # type: ignore[index]
        self.assertEqual(payload["groupKinds"]["站牌"], "mixed")
        self.assertEqual(
            [item["type"] for item in payload["groups"]["站牌"]],
            ["station", "boarding"],
        )

    def test_matching_v1_projection_preserves_hidden_item_positions(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        station = {
            "type": "station",
            "provider": "tpe",
            "stationId": "S1",
            "stationName": "站牌",
        }
        self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:17:10Z",
                "payload": {
                    "groupKinds": {"通勤": "mixed"},
                    "groups": {
                        "通勤": [
                            {
                                "type": "boarding",
                                "provider": "tpe",
                                "routeKey": 1,
                                "pathId": 0,
                                "stopId": 1,
                            },
                            station,
                            {
                                "type": "boarding",
                                "provider": "tpe",
                                "routeKey": 2,
                                "pathId": 0,
                                "stopId": 2,
                            },
                        ]
                    },
                },
            },
        )
        incoming = [
            {"provider": "tpe", "routeKey": 3, "pathId": 0, "stopId": 3},
            {"provider": "tpe", "routeKey": 4, "pathId": 0, "stopId": 4},
        ]

        _, result = self._put_document(
            "favorites",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T03:17:20Z",
                "base_revision": 1,
                "payload": {"groups": {"通勤": incoming}},
            },
        )

        items = result["document"]["payload"]["groups"]["通勤"]  # type: ignore[index]
        self.assertEqual([item["type"] for item in items], ["boarding", "station", "boarding"])
        self.assertEqual(items[0], {"type": "boarding", **incoming[0]})
        self.assertEqual(items[1], station)
        self.assertEqual(items[2], {"type": "boarding", **incoming[1]})

    def test_stale_v1_merge_cannot_delete_v2_data(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        boarding = {
            "type": "boarding",
            "provider": "tpe",
            "routeKey": 5,
            "pathId": 0,
            "stopId": 6,
        }
        self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:18:00Z",
                "payload": {
                    "groupKinds": {"綜合": "mixed"},
                    "groups": {
                        "綜合": [
                            {
                                "type": "route",
                                "provider": "tpe",
                                "routeKey": 7,
                                "routeId": "R7",
                                "routeName": "7",
                            },
                            boarding,
                        ]
                    },
                },
            },
        )
        self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:19:00Z",
                "base_revision": 1,
                "payload": {
                    "groupKinds": {"綜合": "mixed", "空站牌": "station"},
                    "groups": {"綜合": [boarding], "空站牌": []},
                },
            },
        )
        legacy_new = {"provider": "tpe", "routeKey": 8, "pathId": 1, "stopId": 9}

        _, result = self._put_document(
            "favorites",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T03:20:00Z",
                "base_revision": 1,
                "conflict_policy": "merge",
                "payload": {"groups": {"綜合": [legacy_new]}},
            },
        )

        payload = result["document"]["payload"]  # type: ignore[index]
        types = [item["type"] for item in payload["groups"]["綜合"]]
        self.assertEqual(types, ["boarding", "boarding"])
        self.assertEqual(payload["groupKinds"]["綜合"], "mixed")
        self.assertEqual(payload["groupKinds"]["空站牌"], "station")

    def test_v2_merge_same_group_with_different_kinds_becomes_mixed(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        route = {
            "type": "route",
            "provider": "tpe",
            "routeKey": 11,
            "routeId": "R11",
            "routeName": "11",
        }
        self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:21:00Z",
                "payload": {
                    "groupKinds": {"同名": "route"},
                    "groups": {"同名": [route]},
                },
            },
        )
        self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:22:00Z",
                "base_revision": 1,
                "payload": {
                    "groupKinds": {"同名": "route", "另一組": "route"},
                    "groups": {"同名": [route], "另一組": []},
                },
            },
        )
        station = {
            "type": "station",
            "provider": "tpe",
            "stationId": "S12",
            "stationName": "12站",
        }

        _, result = self._put_document(
            "favorites",
            {
                "schema_version": 2,
                "client_modified_at": "2026-05-21T03:23:00Z",
                "base_revision": 1,
                "conflict_policy": "merge",
                "payload": {
                    "groupKinds": {"同名": "station"},
                    "groups": {"同名": [station]},
                },
            },
        )

        payload = result["document"]["payload"]  # type: ignore[index]
        self.assertEqual(payload["groupKinds"]["同名"], "mixed")
        self.assertEqual(
            [item["type"] for item in payload["groups"]["同名"]],
            ["route", "station"],
        )
        self.assertEqual(payload["groupKinds"]["另一組"], "route")

    def test_favorites_rejects_unknown_schema_duplicate_identity_and_cross_type_limit(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)

        with self.assertRaises(HTTPException) as unknown:
            self._put_document(
                "favorites",
                {
                    "schema_version": 3,
                    "client_modified_at": "2026-05-21T03:24:00Z",
                    "payload": {"groups": {}},
                },
            )
        self.assertEqual(unknown.exception.status_code, 422)

        duplicate_routes = [
            {
                "type": "route",
                "provider": "tpe",
                "routeKey": route_key,
                "routeId": "SAME",
                "routeName": "同一路線",
            }
            for route_key in (1, 2)
        ]
        with self.assertRaises(HTTPException) as duplicate:
            self._put_document(
                "favorites",
                {
                    "schema_version": 2,
                    "client_modified_at": "2026-05-21T03:25:00Z",
                    "payload": {
                        "groupKinds": {"路線": "route"},
                        "groups": {"路線": duplicate_routes},
                    },
                },
            )
        self.assertEqual(duplicate.exception.status_code, 422)
        self.assertIn("duplicate favorites", duplicate.exception.detail)

        routes = [
            {
                "type": "route",
                "provider": "tpe",
                "routeKey": index + 1,
                "routeId": f"R{index}",
                "routeName": str(index),
            }
            for index in range(9)
        ]
        stations = [
            {
                "type": "station",
                "provider": "tpe",
                "stationId": f"S{index}",
                "stationName": str(index),
            }
            for index in range(9)
        ]
        boardings = [
            {
                "type": "boarding",
                "provider": "tpe",
                "routeKey": index + 100,
                "pathId": 0,
                "stopId": index + 200,
            }
            for index in range(8)
        ]
        with self.assertRaises(HTTPException) as limit:
            self._put_document(
                "favorites",
                {
                    "schema_version": 2,
                    "client_modified_at": "2026-05-21T03:26:00Z",
                    "payload": {
                        "groupKinds": {"全部": "mixed"},
                        "groups": {"全部": routes + stations + boardings},
                    },
                },
            )
        self.assertEqual(limit.exception.status_code, 422)
        self.assertIn("maximum of 25 items", limit.exception.detail)

    def test_request_size_limit_is_enforced(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)
        small_settings = _settings(self.db_path, account_sync_max_payload_bytes=80)

        oversized_body = {
            "schema_version": 1,
            "client_modified_at": "2026-05-21T03:00:00Z",
            "payload": {"themeMode": "dark", "note": "x" * 200},
        }

        with self.assertRaises(HTTPException) as context:
            self._put_document("preferences", oversized_body, settings=small_settings)

        self.assertEqual(context.exception.status_code, 413)
        self.assertIn("configured limit", context.exception.detail)

    def test_preferences_merge_preserves_disjoint_changes(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)

        first_response, first_result = self._put_document(
            "preferences",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T04:00:00Z",
                "payload": {
                    "themeMode": "dark",
                },
            },
        )
        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(first_result["document"]["revision"], 1)  # type: ignore[index]

        second_response, second_result = self._put_document(
            "preferences",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T04:05:00Z",
                "base_revision": 1,
                "payload": {
                    "themeMode": "dark",
                    "appUpdateCheckMode": "popup",
                },
            },
        )
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_result["document"]["revision"], 2)  # type: ignore[index]

        merge_response, merge_result = self._put_document(
            "preferences",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T04:06:00Z",
                "base_revision": 1,
                "conflict_policy": "merge",
                "payload": {
                    "themeMode": "dark",
                    "mobileMapProvider": "googleMaps",
                },
            },
        )

        self.assertEqual(merge_response.status_code, 200)
        self.assertEqual(merge_result["status"], "merged")
        self.assertEqual(merge_result["document"]["revision"], 3)  # type: ignore[index]
        self.assertEqual(
            merge_result["document"]["payload"],  # type: ignore[index]
            {
                "themeMode": "dark",
                "appUpdateCheckMode": "popup",
                "mobileMapProvider": "googleMaps",
            },
        )

    def test_preferences_conflict_returns_resolution_options(self) -> None:
        account_id = self._create_account()
        self._set_principal(account_id)

        self._put_document(
            "preferences",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T05:00:00Z",
                "payload": {"themeMode": "dark"},
            },
        )
        self._put_document(
            "preferences",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T05:05:00Z",
                "base_revision": 1,
                "payload": {"themeMode": "light"},
            },
        )

        conflict_response, conflict_result = self._put_document(
            "preferences",
            {
                "schema_version": 1,
                "client_modified_at": "2026-05-21T05:06:00Z",
                "base_revision": 1,
                "payload": {"themeMode": "system"},
            },
        )

        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_result["status"], "conflict")
        self.assertEqual(
            conflict_result["resolution_options"],
            ["client_wins", "server_wins", "merge"],
        )
        self.assertEqual(conflict_result["server_document"]["revision"], 2)
        self.assertEqual(conflict_result["merge_preview"]["status"], "not_possible")


if __name__ == "__main__":
    unittest.main()
