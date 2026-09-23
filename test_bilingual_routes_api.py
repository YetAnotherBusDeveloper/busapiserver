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


class BilingualRoutesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(self.db_path)

        app = FastAPI()
        app.state.settings = SimpleNamespace(db_path=self.db_path)
        app.include_router(router)
        self.client = TestClient(app)

        with get_connection(self.db_path) as connection:
            with connection:
                connection.executemany(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    [
                        ("TPE0001", "機場快線", "Airport Express"),
                        ("TPE0002", "機場慢車", "Downtown via Airport Express"),
                        ("TPE0003", "海線", "Coastal Line"),
                        ("TPE0004", "山線", "Mountain Line"),
                    ],
                )
                connection.executemany(
                    "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    [
                        ("TPE0001", 0, "往機場", "To Airport"),
                        ("TPE0002", 0, "往市區", "To Downtown"),
                        ("TPE0003", 0, "機場轉運站", "Airport Express"),
                        ("TPE0004", 0, "機場外環", "Airport Express Outer Loop"),
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO stops
                        (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, 0, 1, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("TPE0001", "STOP-1", "第一航廈", "Terminal 1", 25.0, 121.5),
                        ("TPE0002", "STOP-2", "市政府", "City Hall", 25.1, 121.6),
                        ("TPE0003", "STOP-3", "海邊", "Seaside", 25.2, 121.7),
                        ("TPE0004", "STOP-4", "山腳", "Foothills", 25.3, 121.8),
                    ],
                )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_global_route_search_matches_and_ranks_english_route_and_path_names(self) -> None:
        response = self.client.get("/api/v1/routes?query=airport%20express")

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(
            [row["routeid"] for row in rows],
            ["TPE0001", "TPE0002", "TPE0003", "TPE0004"],
        )
        self.assertEqual(rows[0]["route_name"], "機場快線")
        self.assertEqual(rows[0]["route_name_en"], "Airport Express")
        self.assertEqual(rows[0]["path_name"], "往機場")
        self.assertEqual(rows[0]["path_name_en"], "To Airport")

    def test_city_route_search_matches_english_path_name(self) -> None:
        response = self.client.get(
            "/api/v1/cities/TPE/routes?query=outer%20loop"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["routeid"] for row in response.json()],
            ["TPE0004"],
        )

    def test_nearby_stops_return_bilingual_stop_route_and_path_labels(self) -> None:
        response = self.client.get(
            "/api/v1/cities/TPE/stops/nearby?lat=25&lon=121.5&radius=100"
        )

        self.assertEqual(response.status_code, 200)
        row = response.json()[0]
        self.assertEqual(row["stop_name"], "第一航廈")
        self.assertEqual(row["stop_name_en"], "Terminal 1")
        self.assertEqual(row["route_name"], "機場快線")
        self.assertEqual(row["route_name_en"], "Airport Express")
        self.assertEqual(row["path_name"], "往機場")
        self.assertEqual(row["path_name_en"], "To Airport")

    def test_route_stops_return_bilingual_route_path_and_stop_labels(self) -> None:
        response = self.client.get("/api/v1/routes/TPE0001/stops")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["name"], "機場快線")
        self.assertEqual(body["name_en"], "Airport Express")
        self.assertEqual(body["paths"][0]["name"], "往機場")
        self.assertEqual(body["paths"][0]["name_en"], "To Airport")
        self.assertEqual(body["paths"][0]["stops"][0]["name"], "第一航廈")
        self.assertEqual(body["paths"][0]["stops"][0]["name_en"], "Terminal 1")


if __name__ == "__main__":
    unittest.main()
