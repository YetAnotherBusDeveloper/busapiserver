"""Tests for resolving routeids that a direction merge absorbed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.config import Settings
from app.db import get_connection, init_db
from app.rate_limit import reset_rate_limit_state
from app.route_aliases import get_route_alias_index, reset_route_alias_cache
from app.sync_realtime import RealtimeService, RouteBusesService, _tdx_routeid_to_local
from app.tdx_client import TDXClient, TDXJSONResponse


CANONICAL = "INTTHB181501"
ABSORBED = "INTTHB181502"


def _settings(temp_dir: str, db_path: Path, app_db_path: Path) -> Settings:
    return Settings(
        project_dir=Path(temp_dir),
        db_path=db_path,
        download_db_path=Path(temp_dir) / "downloads" / "bus.db",
        tdx_client_id="test",
        tdx_client_secret="test",
        tdx_base_url="https://example.invalid",
        tdx_token_url="https://example.invalid/token",
        tdx_cities=("Taichung", "NewTaipei"),
        tdx_request_timeout=30,
        tdx_token_refresh_skew=300,
        tdx_retry_attempts=1,
        tdx_retry_backoff=1.0,
        tdx_min_request_interval=0.0,
        realtime_cache_ttl=5,
        realtime_track_ttl=30,
        cors_origins=(),
        auth_public_base_url="https://bus.example.invalid",
        auth_state_ttl_seconds=600,
        auth_snowflake_node_id=0,
        discord_oauth_client_id=None,
        discord_oauth_client_secret=None,
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_native_oauth_client_ids=(),
        app_db_path=app_db_path,
    )


def _seed_merged_route(db_path: Path) -> None:
    """A 公路客運 route whose two directions were merged into one row."""
    with get_connection(db_path) as connection:
        with connection:
            connection.execute(
                "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                (CANONICAL, "1815", "1815"),
            )
            connection.executemany(
                "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                [
                    (CANONICAL, 0, "金青中心", "Jinshan Youth Activity Centre"),
                    (CANONICAL, 1, "臺北車站(東三門)", "Taipei Station (East Gate)"),
                ],
            )
            connection.executemany(
                """
                INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (CANONICAL, 0, 1, "300516", "臺北車站(東三門)", "Taipei Sta.", 25.0479, 121.5184),
                    (CANONICAL, 0, 2, "269906", "金青中心", "Jinshan", 25.2255, 121.6427),
                    (CANONICAL, 1, 1, "269906", "金青中心", "Jinshan", 25.2255, 121.6427),
                    (CANONICAL, 1, 2, "300516", "臺北車站(東三門)", "Taipei Sta.", 25.0479, 121.5184),
                ],
            )
            connection.executemany(
                "INSERT INTO route_subroutes (subroute_uid, routeid, direction) VALUES (?, ?, ?)",
                [(CANONICAL, CANONICAL, 0), (ABSORBED, CANONICAL, 1)],
            )


class RouteAliasIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_route_alias_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.settings = _settings(
            self.temp_dir.name,
            self.db_path,
            Path(self.temp_dir.name) / "app.db",
        )

    def tearDown(self) -> None:
        reset_route_alias_cache()
        self.temp_dir.cleanup()

    def test_empty_table_behaves_as_identity(self) -> None:
        index = get_route_alias_index(self.settings)
        self.assertEqual(index.canonical(ABSORBED), ABSORBED)
        self.assertEqual(index.subroute_uids(CANONICAL), [CANONICAL])
        self.assertFalse(index.is_alias(ABSORBED))

    def test_absorbed_id_resolves_and_expands(self) -> None:
        _seed_merged_route(self.db_path)
        reset_route_alias_cache()

        index = get_route_alias_index(self.settings)
        self.assertEqual(index.canonical(ABSORBED), CANONICAL)
        self.assertEqual(index.canonical(CANONICAL), CANONICAL)
        self.assertEqual(index.subroute_uids(CANONICAL), [CANONICAL, ABSORBED])
        self.assertTrue(index.is_alias(ABSORBED))
        self.assertFalse(index.is_alias(CANONICAL))

    def test_family_routeids_keeps_what_ambiguity_resolution_drops(self) -> None:
        """An unresolvable RouteUID can still be described by its members."""
        with get_connection(self.db_path) as connection:
            with connection:
                connection.executemany(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    [
                        ("TPE10231", "民權幹線", None),
                        ("TPE162593", "民權幹線去程半", None),
                        ("TPE101320", "234", None),
                    ],
                )
                connection.executemany(
                    "INSERT INTO route_uids (route_uid, direction, routeid) VALUES (?, ?, ?)",
                    [
                        ("TPE10231", 0, "TPE10231"),
                        ("TPE10231", 0, "TPE162593"),
                        ("TPE10132", 0, "TPE101320"),
                    ],
                )
        reset_route_alias_cache()

        index = get_route_alias_index(self.settings)

        self.assertIsNone(index.routeid_for_route_uid("TPE10231", 0))
        self.assertEqual(
            index.family_routeids("TPE10231"), ("TPE10231", "TPE162593")
        )
        self.assertEqual(index.family_routeids("TPE10132"), ("TPE101320",))
        self.assertEqual(index.family_routeids("TPE99999"), ())

    def test_unknown_routeid_passes_through(self) -> None:
        _seed_merged_route(self.db_path)
        reset_route_alias_cache()

        index = get_route_alias_index(self.settings)
        self.assertEqual(index.canonical("TPE118150"), "TPE118150")
        self.assertEqual(index.subroute_uids("TPE118150"), ["TPE118150"])


class TdxOutboundExpansionTests(unittest.TestCase):
    """TDX realtime filters on SubRouteUID, so both halves must be requested."""

    def setUp(self) -> None:
        reset_route_alias_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.settings = _settings(
            self.temp_dir.name,
            self.db_path,
            Path(self.temp_dir.name) / "app.db",
        )
        self.client = TDXClient(self.settings, token_manager=None)

    def tearDown(self) -> None:
        reset_route_alias_cache()
        self.temp_dir.cleanup()

    def test_merged_route_expands_to_both_subroute_uids(self) -> None:
        _seed_merged_route(self.db_path)
        reset_route_alias_cache()

        normalized = self.client._normalize_routeids_for_city("InterCity", [CANONICAL])

        # INT prefix stripped for TDX, both directions present.
        self.assertEqual(normalized, ["THB181501", "THB181502"])
        odata = self.client._build_subroute_filter(normalized)
        self.assertIn("SubRouteUID eq 'THB181501'", odata)
        self.assertIn("SubRouteUID eq 'THB181502'", odata)

    def test_requesting_both_halves_does_not_duplicate(self) -> None:
        _seed_merged_route(self.db_path)
        reset_route_alias_cache()

        normalized = self.client._normalize_routeids_for_city(
            "InterCity", [CANONICAL, ABSORBED]
        )
        self.assertEqual(normalized, ["THB181501", "THB181502"])

    def test_unmerged_city_route_is_unchanged(self) -> None:
        normalized = self.client._normalize_routeids_for_city("Taipei", ["TPE118150"])
        self.assertEqual(normalized, ["TPE118150"])


class TdxInboundCollapseTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_route_alias_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.settings = _settings(
            self.temp_dir.name,
            self.db_path,
            Path(self.temp_dir.name) / "app.db",
        )
        _seed_merged_route(self.db_path)
        reset_route_alias_cache()

    def tearDown(self) -> None:
        reset_route_alias_cache()
        self.temp_dir.cleanup()

    def test_absorbed_subroute_uid_collapses_onto_canonical(self) -> None:
        self.assertEqual(
            _tdx_routeid_to_local("InterCity", "THB181502", settings=self.settings),
            CANONICAL,
        )

    def test_without_settings_only_the_int_prefix_is_applied(self) -> None:
        self.assertEqual(_tdx_routeid_to_local("InterCity", "THB181502"), ABSORBED)


class _FakeTDXClient:
    def __init__(self) -> None:
        self.eta_payload_by_route: dict[str, list[dict]] = {}
        self.requested_routeids: list[list[str]] = []

    def fetch_estimated_time_of_arrival_batch(
        self,
        city: str,
        routeids: list[str],
        *,
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
        self.requested_routeids.append(list(routeids))
        payload: list[dict] = []
        for routeid in routeids:
            payload.extend(self.eta_payload_by_route.get(routeid, []))
        return TDXJSONResponse(payload=payload, status_code=200, last_modified=None)

    def fetch_estimated_time_of_arrival(self, city: str, routeid: str) -> list[dict]:
        return list(self.eta_payload_by_route.get(routeid, []))

    def fetch_realtime_by_frequency_batch(
        self,
        city: str,
        routeids: list[str],
        *,
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
        return TDXJSONResponse(payload=[], status_code=200, last_modified=None)

    def fetch_realtime_by_frequency(self, city: str, routeid: str) -> list[dict]:
        return []


class MergedRouteRealtimeTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_route_alias_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.settings = _settings(
            self.temp_dir.name,
            self.db_path,
            Path(self.temp_dir.name) / "app.db",
        )
        _seed_merged_route(self.db_path)
        reset_route_alias_cache()

        self.client = _FakeTDXClient()
        route_buses_service = RouteBusesService(self.settings, self.client)
        self.service = RealtimeService(
            self.settings,
            self.client,
            route_buses_service=route_buses_service,
        )

    def tearDown(self) -> None:
        reset_route_alias_cache()
        self.temp_dir.cleanup()

    def _seed_return_direction_eta(self) -> None:
        # TDX answers with the SubRouteUID that was absorbed, tagged Direction 1.
        self.client.eta_payload_by_route[CANONICAL] = [
            {
                "RouteUID": "THB1815",
                "SubRouteUID": "THB181502",
                "Direction": 1,
                "StopID": "269906",
                "EstimateTime": 300,
                "StopStatus": 0,
            }
        ]

    def test_return_direction_eta_lands_on_the_merged_route(self) -> None:
        self._seed_return_direction_eta()

        snapshot = self.service.get_snapshot(CANONICAL)

        self.assertEqual(snapshot["routeid"], CANONICAL)
        paths = {path["pathid"]: path for path in snapshot["paths"]}
        self.assertEqual(sorted(paths), [0, 1])
        return_stop = next(
            stop for stop in paths[1]["stops"] if stop["stopid"] == "269906"
        )
        self.assertIsNotNone(return_stop["eta"])

    def test_absorbed_routeid_still_resolves(self) -> None:
        self._seed_return_direction_eta()

        snapshot = self.service.get_snapshot(ABSORBED)

        # The response reports the surviving routeid so clients can self-heal.
        self.assertEqual(snapshot["routeid"], CANONICAL)

    def test_batch_echoes_the_snapshot_under_the_requested_alias(self) -> None:
        self._seed_return_direction_eta()

        batch = self.service.get_batch_snapshots([ABSORBED])

        self.assertIn(ABSORBED, batch)
        self.assertIn(CANONICAL, batch)
        self.assertEqual(batch[ABSORBED]["routeid"], CANONICAL)

        # The all-cached fast path must echo aliases too, without another fetch.
        with patch.object(self.client, "fetch_estimated_time_of_arrival_batch") as fetch:
            cached = self.service.get_batch_snapshots([ABSORBED])
        self.assertEqual(cached[ABSORBED], cached[CANONICAL])
        fetch.assert_not_called()


class AliasApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        reset_route_alias_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        _seed_merged_route(self.db_path)
        reset_route_alias_cache()

        app = FastAPI()
        app.state.settings = SimpleNamespace(db_path=self.db_path)
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        reset_route_alias_cache()
        self.temp_dir.cleanup()

    def test_stops_for_an_absorbed_routeid_returns_the_merged_route(self) -> None:
        response = self.client.get(f"/api/v1/routes/{ABSORBED}/stops")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["routeid"], CANONICAL)
        self.assertEqual([path["pathid"] for path in body["paths"]], [0, 1])
        self.assertEqual(response.headers.get("X-Route-Alias-Resolved"), ABSORBED)

    def test_canonical_request_has_no_alias_header(self) -> None:
        response = self.client.get(f"/api/v1/routes/{CANONICAL}/stops")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("X-Route-Alias-Resolved"))

    def test_path_points_keep_their_pathid(self) -> None:
        response = self.client.get(f"/api/v1/routes/{ABSORBED}/paths/1/points")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["routeid"], CANONICAL)
        self.assertEqual(body["pathid"], 1)

    def test_unknown_routeid_still_404s(self) -> None:
        response = self.client.get("/api/v1/routes/INTTHB999999/stops")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
