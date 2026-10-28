from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.db import get_connection, init_db
from app.rate_limit import reset_rate_limit_state


class _FakeRealtimeService:
    """Records the routeids asked for and returns canned snapshots."""

    def __init__(self, snapshots: dict[str, dict]) -> None:
        self.snapshots = snapshots
        self.requested_routeids: list[str] | None = None

    def get_batch_snapshots(self, routeids: list[str]) -> dict[str, dict]:
        self.requested_routeids = list(routeids)
        return {
            routeid: self.snapshots[routeid]
            for routeid in routeids
            if routeid in self.snapshots
        }


class StopPassbyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)

        self.service = _FakeRealtimeService({})
        app = FastAPI()
        app.state.settings = SimpleNamespace(db_path=self.db_path)
        app.state.realtime_service = self.service
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_route(
        self,
        routeid: str,
        route_name: str,
        stops: list[tuple[int, int, str, str]],
        *,
        pathname: str = "Outbound",
        route_name_en: str | None = None,
        path_name_en: str | None = None,
        stop_name_en: str | None = None,
    ) -> None:
        """stops: list of (pathid, seq, stopid, stop_name)."""
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT OR IGNORE INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (routeid, route_name, route_name_en or route_name),
                )
                pathids = {pathid for pathid, _, _, _ in stops}
                for pathid in pathids:
                    connection.execute(
                        "INSERT OR IGNORE INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                        (routeid, pathid, pathname, path_name_en or pathname),
                    )
                connection.executemany(
                    """
                    INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            routeid,
                            pathid,
                            seq,
                            stopid,
                            stop_name,
                            stop_name_en or stop_name,
                            25.0,
                            121.5,
                        )
                        for pathid, seq, stopid, stop_name in stops
                    ],
                )

    def _snapshot(self, routeid: str, pathid: int, stops: list[dict]) -> dict:
        return {
            "routeid": routeid,
            "updated_at": 1000,
            "paths": [{"pathid": pathid, "name": "Outbound", "stops": stops}],
        }

    def test_returns_only_target_stop_bucket_per_route(self) -> None:
        # Two routes both stop at SHARED; each also has unrelated stops that
        # must NOT appear in the response.
        self._seed_route(
            "TPE0001",
            "307",
            [
                (0, 1, "OTHER1", "Other 1"),
                (0, 2, "SHARED", "Shared Stop"),
                (0, 3, "OTHER2", "Other 2"),
            ],
        )
        self._seed_route(
            "TPE0002",
            "308",
            [
                (0, 5, "SHARED", "Shared Stop"),
                (0, 6, "OTHER3", "Other 3"),
            ],
        )
        self.service.snapshots = {
            "TPE0001": self._snapshot(
                "TPE0001",
                0,
                [
                    {"stopid": "OTHER1", "eta": 30, "message": "", "updated_at": 900, "buses": [], "etas": []},
                    {"stopid": "SHARED", "eta": 120, "message": "", "updated_at": 950, "buses": [], "etas": []},
                    {"stopid": "OTHER2", "eta": 300, "message": "", "updated_at": 950, "buses": [], "etas": []},
                ],
            ),
            "TPE0002": self._snapshot(
                "TPE0002",
                0,
                [
                    {"stopid": "SHARED", "eta": None, "message": "尚未發車", "updated_at": 800, "buses": [], "etas": []},
                ],
            ),
        }

        response = self.client.get("/api/v1/stops/SHARED/passby")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stopid"], "SHARED")
        self.assertEqual(body["stop_name"], "Shared Stop")
        self.assertEqual(len(body["routes"]), 2)

        by_routeid = {route["routeid"]: route for route in body["routes"]}
        self.assertEqual(by_routeid["TPE0001"]["eta"], 120)
        self.assertEqual(by_routeid["TPE0001"]["seq"], 2)
        self.assertEqual(by_routeid["TPE0001"]["route_name"], "307")
        self.assertEqual(by_routeid["TPE0002"]["eta"], None)
        self.assertEqual(by_routeid["TPE0002"]["message"], "尚未發車")

        # Only the routes serving the stop should be requested from TDX.
        self.assertEqual(sorted(self.service.requested_routeids), ["TPE0001", "TPE0002"])

    def test_multiple_paths_of_same_route_are_distinct_entries(self) -> None:
        self._seed_route(
            "TPE0003",
            "藍1",
            [
                (0, 4, "SHARED", "Shared Stop"),
                (1, 9, "SHARED", "Shared Stop"),
            ],
        )
        self.service.snapshots = {
            "TPE0003": {
                "routeid": "TPE0003",
                "updated_at": 1000,
                "paths": [
                    {
                        "pathid": 0,
                        "name": "Outbound",
                        "stops": [
                            {"stopid": "SHARED", "eta": 60, "message": "", "updated_at": 950, "buses": [], "etas": []},
                        ],
                    },
                    {
                        "pathid": 1,
                        "name": "Inbound",
                        "stops": [
                            {"stopid": "SHARED", "eta": 600, "message": "", "updated_at": 950, "buses": [], "etas": []},
                        ],
                    },
                ],
            },
        }

        response = self.client.get("/api/v1/stops/SHARED/passby")

        self.assertEqual(response.status_code, 200)
        routes = response.json()["routes"]
        self.assertEqual(len(routes), 2)
        etas_by_path = {route["pathid"]: route["eta"] for route in routes}
        self.assertEqual(etas_by_path, {0: 60, 1: 600})

    def test_returns_bilingual_stop_route_and_path_labels(self) -> None:
        self._seed_route(
            "TPE0005",
            "機場快線",
            [(0, 1, "AIRPORT", "第一航廈")],
            pathname="往機場",
            route_name_en="Airport Express",
            path_name_en="To Airport",
            stop_name_en="Terminal 1",
        )

        response = self.client.get("/api/v1/stops/AIRPORT/passby?city=TPE")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stop_name"], "第一航廈")
        self.assertEqual(body["stop_name_en"], "Terminal 1")
        self.assertEqual(body["routes"][0]["route_name"], "機場快線")
        self.assertEqual(body["routes"][0]["route_name_en"], "Airport Express")
        self.assertEqual(body["routes"][0]["path_name"], "往機場")
        self.assertEqual(body["routes"][0]["path_name_en"], "To Airport")

    def test_missing_snapshot_yields_empty_eta_fields(self) -> None:
        self._seed_route(
            "TPE0004",
            "0東",
            [(0, 1, "SHARED", "Shared Stop")],
        )
        # No snapshot returned for the route (e.g. TDX had nothing).
        self.service.snapshots = {}

        response = self.client.get("/api/v1/stops/SHARED/passby")

        self.assertEqual(response.status_code, 200)
        routes = response.json()["routes"]
        self.assertEqual(len(routes), 1)
        self.assertIsNone(routes[0]["eta"])
        self.assertEqual(routes[0]["message"], "")
        self.assertEqual(routes[0]["buses"], [])

    def test_unknown_stop_returns_404(self) -> None:
        response = self.client.get("/api/v1/stops/NOPE/passby")
        self.assertEqual(response.status_code, 404)

    def test_invalid_stopid_returns_400(self) -> None:
        response = self.client.get("/api/v1/stops/bad%20id/passby")
        self.assertEqual(response.status_code, 400)

    def _seed_colliding_cities(self) -> None:
        # Numeric TDX StopIDs are only unique per authority: Taichung's "39"
        # and Matsu's "39" are different physical stops that share an id.
        self._seed_route("TXG0001", "1", [(0, 3, "39", "中臺科技大學")])
        self._seed_route("LIE16", "海線", [(0, 1, "39", "福澳碼頭")])

    def test_city_prefix_scopes_colliding_stopids(self) -> None:
        self._seed_colliding_cities()

        response = self.client.get("/api/v1/stops/39/passby?city=TXG")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stop_name"], "中臺科技大學")
        self.assertEqual(
            [route["routeid"] for route in body["routes"]],
            ["TXG0001"],
        )
        self.assertEqual(self.service.requested_routeids, ["TXG0001"])

    def test_city_prefix_is_case_insensitive(self) -> None:
        self._seed_colliding_cities()

        response = self.client.get("/api/v1/stops/39/passby?city=lie")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stop_name"], "福澳碼頭")
        self.assertEqual(
            [route["routeid"] for route in body["routes"]],
            ["LIE16"],
        )

    def test_omitted_city_keeps_legacy_nationwide_lookup(self) -> None:
        self._seed_colliding_cities()

        response = self.client.get("/api/v1/stops/39/passby")

        self.assertEqual(response.status_code, 200)
        routeids = {route["routeid"] for route in response.json()["routes"]}
        self.assertEqual(routeids, {"TXG0001", "LIE16"})

    def test_unknown_city_prefix_returns_400(self) -> None:
        self._seed_colliding_cities()

        response = self.client.get("/api/v1/stops/39/passby?city=XXX")

        self.assertEqual(response.status_code, 400)

    def test_scoped_lookup_misses_return_404(self) -> None:
        self._seed_colliding_cities()

        # The stop exists, but not in the requested city.
        response = self.client.get("/api/v1/stops/39/passby?city=TPE")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
