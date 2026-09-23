"""Tests for the whole-city live bus feed backing 全公車地圖.

The interesting cases are all about not lying to the client: an unresolvable
雙北 RouteUID must never be dressed up as a routeid, a 304 must not blank the
map, a plate serving two routes must not collapse into one bus, and the map's
polling must not eat the rest of the app's rate-limit budget.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.city_buses import router as city_buses_router
from app.city_buses import CityBusesService
from app.config import Settings
from app.db import get_connection, init_db
from app.rate_limit import check_rate_limit, reset_rate_limit_state
from app.route_aliases import reset_route_alias_cache
from app.tdx_client import TDXJSONResponse


def _settings(temp_dir: str, db_path: Path, **overrides) -> Settings:
    kwargs = dict(
        project_dir=Path(temp_dir),
        db_path=db_path,
        download_db_path=Path(temp_dir) / "downloads" / "bus.db",
        tdx_client_id="test",
        tdx_client_secret="test",
        tdx_base_url="https://example.invalid",
        tdx_token_url="https://example.invalid/token",
        tdx_cities=("Taipei", "NewTaipei", "Taichung"),
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
        app_db_path=Path(temp_dir) / "app.db",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _bus_item(
    plate: str,
    *,
    route_uid: str,
    subroute_uid: str | None = None,
    direction: int = 0,
    lat: float = 25.03,
    lon: float = 121.56,
    gps_time: str = "2026-09-06T21:41:27+08:00",
    duty_status: int = 0,
) -> dict:
    return {
        "PlateNumb": plate,
        "RouteUID": route_uid,
        "SubRouteUID": subroute_uid,
        "Direction": direction,
        "DutyStatus": duty_status,
        "BusStatus": 0,
        "Speed": 23,
        "Azimuth": 90,
        "GPSTime": gps_time,
        "BusPosition": {"PositionLat": lat, "PositionLon": lon},
    }


class _FakeTDXClient:
    """Records calls and replays programmed responses."""

    def __init__(self) -> None:
        self.responses: list[TDXJSONResponse] = []
        self.calls: list[dict] = []
        self.error: Exception | None = None
        self.default: TDXJSONResponse | None = None
        self.probes: list[int] = []
        self.items_beyond_page: int = 0

    def fetch_city_realtime_buses(
        self,
        city: str,
        *,
        if_modified_since: str | None = None,
        page_size: int = 1000,
    ) -> TDXJSONResponse:
        self.calls.append(
            {
                "city": city,
                "if_modified_since": if_modified_since,
                "page_size": page_size,
            }
        )
        if self.error is not None:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        if self.default is not None:
            return self.default
        return TDXJSONResponse(payload=[], status_code=200, last_modified=None)

    def probe_city_realtime_buses(self, city: str, *, skip: int) -> int:
        self.probes.append(skip)
        return self.items_beyond_page


class CityBusesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        reset_route_alias_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self._seed()
        self.settings = _settings(self.temp_dir.name, self.db_path)
        self.fake = _FakeTDXClient()
        self.service = CityBusesService(self.settings, self.fake)

        app = FastAPI()
        app.state.settings = self.settings
        app.state.city_buses_service = self.service
        app.include_router(city_buses_router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        reset_route_alias_cache()
        reset_rate_limit_state()
        self.temp_dir.cleanup()

    def _seed(self) -> None:
        """One plain route, one ambiguous family, one stub-headed family."""
        with get_connection(self.db_path) as connection:
            with connection:
                connection.executemany(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    [
                        ("TPE101320", "234", "Route 234"),
                        ("TPE10231", "民權幹線", "Minquan Main Line"),
                        ("TPE162593", "民權幹線去程半", "Minquan Outbound Short"),
                        ("TPE162594", "民權幹線返程半", "Minquan Inbound Short"),
                        # Stub: name == routeid, no stops, geometry only.
                        ("TPE10272", "TPE10272", None),
                        ("TPE102720", "303區", None),
                        ("TPE102750", "303經明德新村", None),
                    ],
                )
                connection.executemany(
                    "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    [
                        ("TPE101320", 0, "西門", None),
                        ("TPE10231", 0, "松山車站", None),
                        ("TPE10272", 0, "Unknown", None),
                    ],
                )
                connection.executemany(
                    "INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)"
                    " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                    [
                        ("TPE101320", 0, 1, "33214", "西門", 25.04, 121.50),
                        ("TPE10231", 0, 1, "40001", "民權西路", 25.06, 121.51),
                    ],
                )
                connection.executemany(
                    "INSERT INTO route_uids (route_uid, direction, routeid) VALUES (?, ?, ?)",
                    [
                        ("TPE10132", 0, "TPE101320"),
                        # Ambiguous: the trunk and its two 區間 halves.
                        ("TPE10231", 0, "TPE10231"),
                        ("TPE10231", 0, "TPE162593"),
                        ("TPE10231", 1, "TPE10231"),
                        ("TPE10231", 1, "TPE162594"),
                        # Ambiguous and headed by a stub row.
                        ("TPE10272", 0, "TPE102720"),
                        ("TPE10272", 0, "TPE102750"),
                    ],
                )

    def _serve(self, items: list[dict], *, last_modified: str | None = "MOD-1") -> None:
        self.fake.default = TDXJSONResponse(
            payload=items, status_code=200, last_modified=last_modified
        )

    def _get(self, city: str = "TPE"):
        return self.client.get(f"/api/v1/cities/{city}/buses")


class IdentityTests(CityBusesTestCase):
    def test_resolved_bus_carries_routeid_and_name(self) -> None:
        self._serve([_bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")])

        body = self._get().json()

        self.assertEqual(len(body["buses"]), 1)
        bus = body["buses"][0]
        self.assertEqual(bus["routeid"], "TPE101320")
        self.assertEqual(bus["route_uid"], "TPE10132")
        self.assertEqual(bus["id"], "KKA-1234")
        self.assertEqual(body["routes"]["TPE101320"]["name"], "234")
        self.assertEqual(body["routes"]["TPE101320"]["name_en"], "Route 234")
        self.assertEqual(body["families"], {})

    def test_ambiguous_route_uid_is_unresolved_but_still_shown(self) -> None:
        self._serve([_bus_item("EAL-0562", route_uid="TPE10231", subroute_uid=None)])

        body = self._get().json()

        bus = body["buses"][0]
        self.assertIsNone(bus["routeid"])
        self.assertEqual(bus["route_uid"], "TPE10231")
        family = body["families"]["TPE10231"]
        self.assertEqual(family["name"], "民權幹線")
        self.assertEqual(family["name_en"], "Minquan Main Line")
        self.assertEqual(family["stops_routeid"], "TPE10231")
        self.assertEqual(family["geometry_routeid"], "TPE10231")
        self.assertEqual(
            family["routeids"], ["TPE10231", "TPE162593", "TPE162594"]
        )

    def test_route_uid_is_never_emitted_as_a_routeid(self) -> None:
        self._serve(
            [
                _bus_item("EAL-0562", route_uid="TPE10231", subroute_uid=None),
                _bus_item("EAL-0563", route_uid="TPE99999", subroute_uid=None),
            ]
        )

        body = self._get().json()

        self.assertTrue(all(bus["routeid"] is None for bus in body["buses"]))
        # An unknown RouteUID still yields a describable, if bare, family.
        self.assertEqual(body["families"]["TPE99999"]["name"], "TPE99999")
        self.assertIsNone(body["families"]["TPE99999"]["name_en"])
        self.assertEqual(body["families"]["TPE99999"]["routeids"], [])
        self.assertIsNone(body["families"]["TPE99999"]["stops_routeid"])

    def test_stub_headed_family_keeps_geometry_but_never_stops(self) -> None:
        self._serve([_bus_item("BUS-303", route_uid="TPE10272", subroute_uid=None)])

        family = self._get().json()["families"]["TPE10272"]

        self.assertNotIn("TPE10272", family["routeids"])
        self.assertEqual(family["routeids"], ["TPE102720", "TPE102750"])
        self.assertEqual(family["stops_routeid"], "TPE102720")
        # The stub row is the one that owns the shape, so it draws the line.
        self.assertEqual(family["geometry_routeid"], "TPE10272")

    def test_same_plate_on_two_routes_survives_as_two_buses(self) -> None:
        self._serve(
            [
                _bus_item("SAME-01", route_uid="TPE10132", subroute_uid="TPE101320"),
                _bus_item("SAME-01", route_uid="TPE10231", subroute_uid=None),
            ]
        )

        buses = self._get().json()["buses"]

        self.assertEqual(len(buses), 2)
        self.assertEqual({bus["routeid"] for bus in buses}, {"TPE101320", None})

    def test_same_plate_on_one_route_keeps_the_newest_fix(self) -> None:
        self._serve(
            [
                _bus_item(
                    "SAME-02",
                    route_uid="TPE10132",
                    subroute_uid="TPE101320",
                    gps_time="2026-09-06T21:00:00+08:00",
                    lat=25.00,
                ),
                _bus_item(
                    "SAME-02",
                    route_uid="TPE10132",
                    subroute_uid="TPE101320",
                    gps_time="2026-09-06T21:41:27+08:00",
                    lat=25.09,
                ),
            ]
        )

        buses = self._get().json()["buses"]

        self.assertEqual(len(buses), 1)
        self.assertAlmostEqual(buses[0]["lat"], 25.09)

    def test_off_duty_and_id_less_items_are_dropped(self) -> None:
        self._serve(
            [
                _bus_item(
                    "OFF-01",
                    route_uid="TPE10132",
                    subroute_uid="TPE101320",
                    duty_status=2,
                ),
                {"PlateNumb": "NOID-1", "Direction": 0, "BusPosition": {}},
            ]
        )

        self.assertEqual(self._get().json()["buses"], [])


class CacheTests(CityBusesTestCase):
    def test_second_request_within_ttl_does_not_hit_upstream(self) -> None:
        self._serve([_bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")])

        self._get()
        self._get()

        self.assertEqual(len(self.fake.calls), 1)

    def test_concurrent_requests_make_one_upstream_call(self) -> None:
        started = threading.Event()

        class _SlowClient(_FakeTDXClient):
            def fetch_city_realtime_buses(self, city, **kwargs):
                started.set()
                time.sleep(0.2)
                return super().fetch_city_realtime_buses(city, **kwargs)

        slow = _SlowClient()
        slow.default = TDXJSONResponse(
            payload=[_bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")],
            status_code=200,
            last_modified="MOD-1",
        )
        service = CityBusesService(self.settings, slow)

        with ThreadPoolExecutor(max_workers=8) as pool:
            payloads = list(
                pool.map(lambda _: service.get_city_buses("Taipei", "TPE"), range(8))
            )

        self.assertEqual(len(slow.calls), 1)
        self.assertTrue(all(payload["buses"] for payload in payloads))

    def test_not_modified_keeps_the_previous_buses(self) -> None:
        settings = _settings(self.temp_dir.name, self.db_path, city_buses_cache_ttl=0)
        service = CityBusesService(settings, self.fake)
        self.fake.responses = [
            TDXJSONResponse(
                payload=[
                    _bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")
                ],
                status_code=200,
                last_modified="MOD-1",
            ),
            TDXJSONResponse(payload=[], status_code=304, last_modified="MOD-1"),
        ]

        first = service.get_city_buses("Taipei", "TPE")
        second = service.get_city_buses("Taipei", "TPE")

        self.assertEqual(len(second["buses"]), 1)
        self.assertEqual(second["buses"], first["buses"])
        self.assertFalse(second["stale"])
        self.assertEqual(self.fake.calls[1]["if_modified_since"], "MOD-1")

    def test_upstream_failure_serves_the_last_snapshot_as_stale(self) -> None:
        settings = _settings(self.temp_dir.name, self.db_path, city_buses_cache_ttl=0)
        service = CityBusesService(settings, self.fake)
        self.fake.default = TDXJSONResponse(
            payload=[_bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")],
            status_code=200,
            last_modified="MOD-1",
        )
        service.get_city_buses("Taipei", "TPE")

        self.fake.error = requests.ConnectionError("boom")
        payload = service.get_city_buses("Taipei", "TPE")

        self.assertTrue(payload["stale"])
        self.assertEqual(len(payload["buses"]), 1)

    def test_upstream_failure_past_stale_max_propagates(self) -> None:
        settings = _settings(
            self.temp_dir.name,
            self.db_path,
            city_buses_cache_ttl=0,
            city_buses_stale_max_seconds=0,
        )
        service = CityBusesService(settings, self.fake)
        self.fake.default = TDXJSONResponse(
            payload=[_bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")],
            status_code=200,
            last_modified="MOD-1",
        )
        service.get_city_buses("Taipei", "TPE")

        self.fake.error = requests.ConnectionError("boom")
        with self.assertRaises(requests.ConnectionError):
            service.get_city_buses("Taipei", "TPE")

    def test_upstream_failure_without_cache_is_a_bad_gateway(self) -> None:
        self.fake.error = requests.ConnectionError("boom")

        response = self._get()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "TDX upstream request failed.")


class TruncationTests(CityBusesTestCase):
    def test_silent_top_cap_is_detected_and_refetched(self) -> None:
        short = [
            _bus_item(f"CAP-{index}", route_uid="TPE10132", subroute_uid="TPE101320")
            for index in range(3)
        ]
        full = short + [
            _bus_item("CAP-3", route_uid="TPE10132", subroute_uid="TPE101320")
        ]
        self.fake.responses = [
            TDXJSONResponse(payload=short, status_code=200, last_modified="MOD-1"),
            TDXJSONResponse(payload=full, status_code=200, last_modified="MOD-1"),
        ]
        self.fake.items_beyond_page = 1

        with self.assertLogs("busapi.city_buses", level="WARNING") as logs:
            payload = self.service.get_city_buses("Taipei", "TPE")

        self.assertTrue(payload["truncated"])
        self.assertEqual(len(payload["buses"]), 4)
        self.assertTrue(any("page cap detected" in line for line in logs.output))
        # One extra item asked for, not a walk through the whole city.
        self.assertEqual(self.fake.probes, [3])
        self.assertEqual(self.fake.calls[-1]["page_size"], 3)

    def test_complete_page_is_not_flagged(self) -> None:
        self._serve(
            [
                _bus_item(f"OK-{index}", route_uid="TPE10132", subroute_uid="TPE101320")
                for index in range(3)
            ]
        )
        # Nothing past the end, so the short page really was the whole city.
        self.fake.items_beyond_page = 0

        payload = self.service.get_city_buses("Taipei", "TPE")

        self.assertFalse(payload["truncated"])
        self.assertEqual(len(payload["buses"]), 3)
        self.assertEqual(len(self.fake.calls), 1)

    def test_sharp_drop_against_the_previous_count_is_flagged(self) -> None:
        self.assertTrue(CityBusesService._looks_truncated(10, 800))
        self.assertFalse(CityBusesService._looks_truncated(700, 800))
        self.assertFalse(CityBusesService._looks_truncated(1, 10))
        self.assertFalse(CityBusesService._looks_truncated(0, None))


class EndpointTests(CityBusesTestCase):
    def test_prefix_and_city_name_resolve_to_the_same_feed(self) -> None:
        self._serve([_bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")])

        for city in ("TPE", "tpe", "Taipei", "taipei"):
            with self.subTest(city=city):
                body = self._get(city).json()
                self.assertEqual(body["city"], "Taipei")
                self.assertEqual(body["prefix"], "TPE")

    def test_unknown_and_unserved_cities_are_not_found(self) -> None:
        self.assertEqual(self._get("XXX").status_code, 404)
        # A real prefix the deployment does not sync must not trigger a TDX pull.
        self.assertEqual(self._get("KHH").status_code, 404)
        self.assertEqual(self.fake.calls, [])

    def test_response_advertises_its_own_ttl_and_cache_window(self) -> None:
        self._serve([_bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")])

        response = self._get()

        self.assertEqual(response.json()["ttl"], self.settings.city_buses_cache_ttl)
        self.assertEqual(
            response.headers["cache-control"],
            f"private, max-age={self.settings.city_buses_cache_ttl}",
        )

    def test_map_polling_cannot_exhaust_the_shared_budget(self) -> None:
        settings = _settings(
            self.temp_dir.name, self.db_path, city_buses_rate_limit_requests=3
        )
        app = FastAPI()
        app.state.settings = settings
        app.state.city_buses_service = self.service
        app.include_router(city_buses_router)
        client = TestClient(app)
        self._serve([_bus_item("KKA-1234", route_uid="TPE10132", subroute_uid="TPE101320")])

        for _ in range(3):
            self.assertEqual(client.get("/api/v1/cities/TPE/buses").status_code, 200)
        blocked = client.get("/api/v1/cities/TPE/buses")

        self.assertEqual(blocked.status_code, 429)
        self.assertIn("retry-after", blocked.headers)
        # The rest of the app still has its full 60/min.
        check_rate_limit("ip:testclient", "global")


if __name__ == "__main__":
    unittest.main()
