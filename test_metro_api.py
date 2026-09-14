from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import metro
from app.rate_limit import reset_rate_limit_state


class _FakeTdxClient:
    def fetch_paginated_items(self, path: str, **_kwargs):
        if "LiveBoard" in path:
            return []
        if "StationTimeTable" in path:
            return [
                {
                    "StationID": "BL01",
                    "StationName": {"Zh_tw": "頂埔"},
                    "Direction": 0,
                    "LineID": "BL",
                    "DestinationStationName": {"Zh_tw": "南港展覽館"},
                    "Timetables": [
                        {
                            "Sequence": 1,
                            "ArrivalTime": "",
                            "DepartureTime": "10:05",
                        }
                    ],
                }
            ]
        raise AssertionError(f"Unexpected TDX path: {path}")


class MetroApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        metro._cache.clear()
        app = FastAPI()
        app.state.tdx_client = _FakeTdxClient()
        app.include_router(metro.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        metro._cache.clear()

    @patch("app.api.metro.datetime")
    def test_trtc_uses_departure_time_when_arrival_time_is_blank(self, mock_datetime) -> None:
        mock_datetime.datetime.now.return_value = datetime(2026, 9, 13, 10, 0, 0)

        response = self.client.get("/api/v1/metro/TRTC/lines/BL/eta")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "timetable")
        self.assertEqual(body["entries"], [
            {
                "station_id": "BL01",
                "station_name": "頂埔",
                "line_id": "BL",
                "direction": 0,
                "destination_name": "南港展覽館",
                "trip_head_sign": "往南港展覽館",
                "estimated_time": 300,
                "next_arrival": "10:05",
                "service_status": 0,
            }
        ])


if __name__ == "__main__":
    unittest.main()
