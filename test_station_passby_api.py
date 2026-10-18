from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
import requests

from app.api.routes import router
from app.db import get_connection, init_db
from app.rate_limit import reset_rate_limit_state
from app.sync_static import _copy_main_routes_by_prefix, _replace_bus_stations


class _FakeRealtimeService:
    def __init__(self) -> None:
        self.snapshots: dict[str, dict] = {}
        self.requested_routeids: list[str] | None = None
        self.error: Exception | None = None

    def get_batch_snapshots(self, routeids: list[str]) -> dict[str, dict]:
        self.requested_routeids = list(routeids)
        if self.error is not None:
            raise self.error
        return {
            routeid: self.snapshots[routeid]
            for routeid in routeids
            if routeid in self.snapshots
        }


class StationPassbyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        self.service = _FakeRealtimeService()

        app = FastAPI()
        app.state.settings = SimpleNamespace(db_path=self.db_path)
        app.state.realtime_service = self.service
        app.include_router(router)
        self.client = TestClient(app)

        # Opposite kerbs, ~220 m apart, matching each side's StopPosition below.
        # They must stay two sides: only genuinely co-located poles get merged.
        self._seed_route("TPE0001", "307", "STOP-A", 0, 2, lat=25.039, lon=121.559)
        self._seed_route("TPE0002", "藍1", "STOP-B", 1, 5, lat=25.041, lon=121.561)
        self._seed_route("KHH0001", "紅1", "STOP-A", 0, 1)
        with get_connection(self.db_path) as connection:
            with connection:
                # Deliberately supply B before A. Side labels must be generated
                # from stable StopUID/StopID sorting, never feed order.
                _replace_bus_stations(
                    connection,
                    "TPE",
                    [
                        {
                            "StationUID": "TPE-STATION-1",
                            "StationName": {"Zh_tw": "市政府", "En": "City Hall"},
                            "StationPosition": {
                                "PositionLat": 25.04,
                                "PositionLon": 121.56,
                            },
                            "Stops": [
                                {
                                    "StopUID": "TPE-UID-B",
                                    "StopID": "STOP-B",
                                    "StopName": {"Zh_tw": "市政府（松高路）"},
                                    "StopPosition": {
                                        "PositionLat": 25.041,
                                        "PositionLon": 121.561,
                                    },
                                },
                                {
                                    "StopUID": "TPE-UID-A",
                                    "StopID": "STOP-A",
                                    "StopName": {"Zh_tw": "市政府（忠孝東路）"},
                                    "StopPosition": {
                                        "PositionLat": 25.039,
                                        "PositionLon": 121.559,
                                    },
                                },
                            ],
                        }
                    ],
                )
                _replace_bus_stations(
                    connection,
                    "KHH",
                    [
                        {
                            "StationUID": "KHH-STATION-1",
                            "StationName": {"Zh_tw": "高雄站"},
                            "StationPosition": {
                                "PositionLat": 22.63,
                                "PositionLon": 120.30,
                            },
                            "Stops": [
                                {"StopUID": "KHH-UID-A", "StopID": "STOP-A"},
                            ],
                        }
                    ],
                )

        self.service.snapshots = {
            "TPE0001": self._snapshot("TPE0001", 0, "STOP-A", 90),
            "TPE0002": self._snapshot("TPE0002", 1, "STOP-B", 300),
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_route(
        self,
        routeid: str,
        route_name: str,
        stopid: str,
        pathid: int,
        seq: int,
        lat: float = 25.04,
        lon: float = 121.56,
    ) -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                # OR IGNORE lets one route be seeded once per direction.
                connection.execute(
                    "INSERT OR IGNORE INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (routeid, route_name, route_name),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    (routeid, pathid, "往終點", "Outbound"),
                )
                connection.execute(
                    """
                    INSERT INTO stops
                        (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (routeid, pathid, seq, stopid, "市政府", "City Hall", lat, lon),
                )

    def _replace_single_stop_stations(
        self, city_code: str, stations: list[tuple[str, str, str, float, float]]
    ) -> None:
        """Seed (station_id, name, stopid, lat, lon) stations, one kerb each."""
        with get_connection(self.db_path) as connection:
            with connection:
                _replace_bus_stations(
                    connection,
                    city_code,
                    [
                        {
                            "StationUID": station_id,
                            "StationName": {"Zh_tw": name},
                            "StationPosition": {
                                "PositionLat": lat,
                                "PositionLon": lon,
                            },
                            "Stops": [
                                {
                                    "StopUID": f"{city_code}{stopid}",
                                    "StopID": stopid,
                                    "StopName": {"Zh_tw": name},
                                }
                            ],
                        }
                        for station_id, name, stopid, lat, lon in stations
                    ],
                )

    @staticmethod
    def _snapshot(routeid: str, pathid: int, stopid: str, eta: int) -> dict:
        return {
            "routeid": routeid,
            "paths": [
                {
                    "pathid": pathid,
                    "stops": [
                        {
                            "stopid": stopid,
                            "eta": eta,
                            "message": "",
                            "updated_at": 1000,
                            "buses": [],
                            "etas": [eta],
                        }
                    ],
                }
            ],
        }

    def test_resolve_returns_stable_station_sides_and_eta_routes(self) -> None:
        response = self.client.get(
            "/api/v1/stations/resolve?city=tpe&stopid=STOP-B"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["city"], "TPE")
        self.assertEqual(body["station_id"], "TPE-STATION-1")
        self.assertEqual(body["station_name"], "市政府")
        self.assertEqual([side["label"] for side in body["sides"]], ["A", "B"])
        self.assertEqual(
            [side["side_id"] for side in body["sides"]],
            ["TPE-UID-A", "TPE-UID-B"],
        )
        self.assertEqual(body["sides"][0]["routes"][0]["routeid"], "TPE0001")
        self.assertEqual(body["sides"][0]["routes"][0]["eta"], 90)
        self.assertEqual(body["sides"][1]["routes"][0]["eta"], 300)
        self.assertEqual(self.service.requested_routeids, ["TPE0001", "TPE0002"])

    def test_passby_returns_station_by_stable_id(self) -> None:
        response = self.client.get(
            "/api/v1/stations/TPE-STATION-1/passby?city=TPE"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["station_name_en"], "City Hall")
        self.assertAlmostEqual(body["lat"], 25.04)
        self.assertEqual(body["sides"][0]["direction"], "市政府（忠孝東路）")

    def test_resolve_still_works_after_a_304_reuse_copy(self) -> None:
        # The all-304 fast path in sync_static rebuilds the database by copying
        # the previous one instead of refetching. This asserts the user-visible
        # end of that: a station favourited before the sync must still resolve
        # afterwards, rather than the app reporting no whole-station data.
        target_path = Path(self.temp_dir.name) / "bus.db.tmp"
        init_db(target_path)
        with get_connection(self.db_path) as source, get_connection(
            target_path
        ) as target:
            with target:
                _copy_main_routes_by_prefix(source, target, "TPE")
        self.client.app.state.settings.db_path = target_path

        response = self.client.get(
            "/api/v1/stations/resolve?city=TPE&stopid=STOP-B"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["station_id"], "TPE-STATION-1")
        self.assertEqual([side["label"] for side in body["sides"]], ["A", "B"])

    def test_co_located_sides_merge_into_one(self) -> None:
        # The 豐樂公園 shape: TDX mints a StopUID per route family, so one kerb
        # arrives as several sides at an identical position. A passenger sees
        # one pole, so the payload must too.
        self._seed_route("TPE0003", "綠3", "STOP-A2", 0, 3, lat=25.039, lon=121.559)
        self._seed_route("TPE0004", "73", "STOP-A3", 0, 4, lat=25.039, lon=121.559)
        with get_connection(self.db_path) as connection:
            with connection:
                _replace_bus_stations(
                    connection,
                    "TPE",
                    [
                        {
                            "StationUID": "TPE-STATION-2",
                            "StationName": {"Zh_tw": "豐樂公園"},
                            "StationPosition": {
                                "PositionLat": 25.039,
                                "PositionLon": 121.559,
                            },
                            "Stops": [
                                {
                                    "StopUID": f"TPE-UID-{s}",
                                    "StopID": s,
                                    "StopName": {"Zh_tw": "豐樂公園"},
                                }
                                for s in ("STOP-A", "STOP-A2", "STOP-A3")
                            ],
                        }
                    ],
                )

        response = self.client.get("/api/v1/stations/TPE-STATION-2/passby?city=TPE")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([side["label"] for side in body["sides"]], ["A"])
        self.assertEqual(
            [route["route_name"] for route in body["sides"][0]["routes"]],
            ["307", "綠3", "73"],
        )
        # Each route still resolves its own stop, which is what ETA lookup uses.
        self.assertEqual(
            [route["stopid"] for route in body["sides"][0]["routes"]],
            ["STOP-A", "STOP-A2", "STOP-A3"],
        )

    def test_far_kerb_station_with_the_same_name_becomes_another_side(self) -> None:
        # The 中和(公安路) shape. TDX's Station is one kerb, so the stop is two
        # same-name Stations, each serving one direction, 8.5 m apart — inside
        # the pole-merge radius, yet they must stay two sides.
        for routeid, name in (("TXG155", "155"), ("TXG92", "92")):
            self._seed_route(routeid, name, "10079", 0, 30, lat=24.310417, lon=120.698049)
            self._seed_route(routeid, name, "10082", 1, 9, lat=24.310371, lon=120.697982)
        self._replace_single_stop_stations(
            "TXG",
            [
                ("TXG6571", "中和(公安路)", "10082", 24.310371, 120.697982),
                ("TXG6572", "中和(公安路)", "10079", 24.310417, 120.698049),
                # Same name in another district is a different stop...
                ("TXG9001", "中和(公安路)", "20001", 24.25, 120.70),
                # ...and so is the neighbouring station under another name.
                ("TXG9002", "公安路口", "20002", 24.3105, 120.6981),
            ],
        )

        picked = self.client.get("/api/v1/stations/TXG6572/passby?city=TXG").json()
        resolved = self.client.get(
            "/api/v1/stations/resolve?city=TXG&stopid=10082"
        ).json()

        self.assertEqual(picked["station_id"], "TXG6572")
        self.assertEqual([side["label"] for side in picked["sides"]], ["A", "B"])
        # Side A is always the kerb that was asked for.
        self.assertEqual(
            [side["stopid"] for side in picked["sides"]], ["10079", "10082"]
        )
        self.assertEqual(
            [
                [(route["route_name"], route["pathid"]) for route in side["routes"]]
                for side in picked["sides"]
            ],
            [[("155", 0), ("92", 0)], [("155", 1), ("92", 1)]],
        )
        self.assertEqual(resolved["station_id"], "TXG6571")
        self.assertEqual(
            [side["stopid"] for side in resolved["sides"]], ["10082", "10079"]
        )

    def test_same_name_stations_chain_into_one_group_from_either_end(self) -> None:
        # ~250 m apart in a row: the ends are ~500 m from each other, but every
        # member must see the same sides, whichever station the rider picked.
        self._replace_single_stop_stations(
            "TXG",
            [
                ("TXG-NORTH", "中友百貨", "S-NORTH", 24.15450, 120.68),
                ("TXG-MID", "中友百貨", "S-MID", 24.15225, 120.68),
                ("TXG-SOUTH", "中友百貨", "S-SOUTH", 24.15000, 120.68),
            ],
        )

        north = self.client.get("/api/v1/stations/TXG-NORTH/passby?city=TXG").json()
        south = self.client.get("/api/v1/stations/TXG-SOUTH/passby?city=TXG").json()

        self.assertEqual(
            [side["stopid"] for side in north["sides"]],
            ["S-NORTH", "S-MID", "S-SOUTH"],
        )
        self.assertEqual(
            [side["stopid"] for side in south["sides"]],
            ["S-SOUTH", "S-MID", "S-NORTH"],
        )

    def test_resolve_is_city_scoped_without_name_or_distance_fallback(self) -> None:
        taipei = self.client.get(
            "/api/v1/stations/resolve?city=TPE&stopid=STOP-A"
        )
        kaohsiung = self.client.get(
            "/api/v1/stations/resolve?city=KHH&stopid=STOP-A"
        )
        missing = self.client.get(
            "/api/v1/stations/resolve?city=TPE&stopid=SIMILAR-NAME"
        )

        self.assertEqual(taipei.json()["station_id"], "TPE-STATION-1")
        self.assertEqual(kaohsiung.json()["station_id"], "KHH-STATION-1")
        self.assertEqual(missing.status_code, 404)

    def test_unknown_city_station_and_invalid_id_are_rejected(self) -> None:
        self.assertEqual(
            self.client.get(
                "/api/v1/stations/resolve?city=XXX&stopid=STOP-A"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/stations/NOPE/passby?city=TPE"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/stations/bad%20id/passby?city=TPE"
            ).status_code,
            400,
        )

    def test_upstream_failure_matches_stop_passby_semantics(self) -> None:
        self.service.error = requests.RequestException("temporary")

        response = self.client.get(
            "/api/v1/stations/TPE-STATION-1/passby?city=TPE"
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "TDX upstream request failed.")


if __name__ == "__main__":
    unittest.main()
