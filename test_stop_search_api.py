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


class StopSearchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)
        app = FastAPI()
        # This feature only needs static data: no TDX client or credentials.
        app.state.settings = SimpleNamespace(db_path=self.db_path)
        app.include_router(router)
        self.client = TestClient(app)
        with get_connection(self.db_path) as connection, connection:
            for routeid, name in (
                ("TPE001", "1"), ("TPE002", "2"), ("TPE003", "3"),
                ("TXG001", "1"), ("INTTHB1815", "1815"), ("NWT100", "100"),
            ):
                connection.execute("INSERT INTO routes VALUES (?, ?, ?)", (routeid, name, f"Route {name}"))
                for pathid in (0, 1):
                    connection.execute("INSERT INTO paths VALUES (?, ?, ?, ?)",
                                       (routeid, pathid, "往市區", "To Downtown"))
            connection.executemany(
                "INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("TPE001", 0, 1, "EAST", "市政府東門", "City Hall East", 25.0, 121.5),
                    ("TPE001", 0, 4, "SHARED", "市政府", "City Hall", 25.01, 121.51),
                    ("TPE001", 0, 7, "LOOP", "市政府", "City Hall", 25.01, 121.51),
                    ("TPE001", 1, 2, "RETURN", "市政府", "City Hall", 25.02, 121.52),
                    ("TPE002", 0, 1, "MRT", "市政府捷運站", "City Hall MRT", 25.0, 121.5),
                    ("TPE003", 0, 1, "NEW", "新市政府", "New City Hall", 25.0, 121.5),
                    ("TXG001", 0, 1, "SHARED", "市政府", "City Hall", 24.1, 120.6),
                    ("INTTHB1815", 1, 3, "0123", "臺北車站", "Taipei Main Station", 25.04, 121.51),
                    ("NWT100", 0, 1, "PERCENT", "100%_交流站", "King's Park", 25.0, 121.5),
                ],
            )

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def search(self, query: str, **params):
        return self.client.get("/api/v1/stops/search", params={"query": query, **params})

    def test_exact_prefix_substring_ranking_and_direction_identity(self) -> None:
        response = self.search("市政府")
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual([(row["routeid"], row["pathid"]) for row in rows],
                         [("TPE001", 0), ("TPE001", 1), ("TXG001", 0), ("TPE002", 0), ("TPE003", 0)])
        self.assertEqual(rows[0]["stopid"], "SHARED")
        self.assertEqual(rows[0]["seq"], 4)
        self.assertEqual(rows[0]["route_name_en"], "Route 1")
        self.assertEqual(rows[0]["path_name_en"], "To Downtown")
        self.assertEqual(rows[0]["stop_name_en"], "City Hall")
        self.assertEqual(rows[2]["city_code"], "TXG")
        self.assertEqual(rows[2]["lat"], 24.1)

    def test_english_case_insensitive_and_city_scopes_are_combined(self) -> None:
        response = self.search("  CITY HALL  ", cities="Taipei,txg,TPE")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 5)
        self.assertEqual({row["city_code"] for row in self.search("市政府", cities="TXG").json()}, {"TXG"})

    def test_tai_variant_intercity_and_original_stop_id(self) -> None:
        for query in ("台北", "臺北", "taipei main station"):
            with self.subTest(query=query):
                response = self.search(query, cities="InterCity")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()[0]["stopid"], "0123")
                self.assertEqual(response.json()[0]["pathid"], 1)

    def test_user_input_is_literal_and_parameterized(self) -> None:
        for query in ("%", "_", "100%_", "King's Park"):
            with self.subTest(query=query):
                self.assertEqual([row["routeid"] for row in self.search(query).json()], ["NWT100"])
        self.assertEqual(self.search("' OR 1=1 --").json(), [])

    def test_limit_is_applied_after_route_direction_deduplication(self) -> None:
        rows = self.search("市政府", limit=3).json()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({(row["routeid"], row["pathid"]) for row in rows}), 3)
        self.assertIn("TXG001", [row["routeid"] for row in rows])

    def test_empty_missing_invalid_and_unmatched_queries(self) -> None:
        self.assertEqual(self.search("   ").json(), [])
        self.assertEqual(self.search("不存在的站牌").json(), [])
        for params in ({"query": ""}, {"query": "x" * 121}, {"query": "市政府", "limit": 101},
                       {"query": "市政府", "limit": 0}, {"query": "市政府", "cities": "Nowhere"},
                       {"query": "市政府", "cities": "Taipei,,Taichung"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/api/v1/stops/search", params=params).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/stops/search").status_code, 422)


if __name__ == "__main__":
    unittest.main()
