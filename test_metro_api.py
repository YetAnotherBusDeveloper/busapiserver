from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import metro
from app.rate_limit import reset_rate_limit_state


class _FakeTdxClient:
    def __init__(self) -> None:
        self.liveboard = []
        self.timetables = [
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

    def fetch_paginated_items(self, path: str, **_kwargs):
        if "LiveBoard" in path:
            return self.liveboard
        if "StationTimeTable" in path:
            return self.timetables
        raise AssertionError(f"Unexpected TDX path: {path}")


class MetroApiTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_rate_limit_state()
        metro._cache.clear()
        app = FastAPI()
        self.tdx = _FakeTdxClient()
        app.state.tdx_client = self.tdx
        app.include_router(metro.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        metro._cache.clear()

    @patch(
        "app.api.metro._taiwan_now",
        return_value=datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    def test_trtc_uses_departure_time_when_arrival_time_is_blank(self, _mock_now) -> None:

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
                "destination_id": "",
                "destination_name": "南港展覽館",
                "trip_head_sign": "往南港展覽館",
                "estimated_time": 300,
                "next_arrival": "10:05",
                "service_status": 0,
            }
        ])

    @patch(
        "app.api.metro._taiwan_now",
        return_value=datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    def test_liveboard_converts_tdx_minutes_to_contract_seconds(self, _mock_now) -> None:
        self.tdx.liveboard = [
            {
                "StationID": "R3",
                "StationName": {"Zh_tw": "小港"},
                "LineID": "R",
                "DestinationStationID": "RK1",
                "DestinationStationName": {"Zh_tw": "岡山車站"},
                "TripHeadSign": "往岡山車站",
                "EstimateTime": 3,
                "ServiceStatus": 0,
            }
        ]

        eta_response = self.client.get("/api/v1/metro/KRTC/lines/R/eta")
        liveboard_response = self.client.get("/api/v1/metro/KRTC/lines/R/liveboard")

        self.assertEqual(eta_response.status_code, 200)
        self.assertEqual(eta_response.json()["source"], "liveboard")
        self.assertEqual(eta_response.json()["entries"][0]["estimated_time"], 180)
        self.assertEqual(liveboard_response.status_code, 200)
        self.assertEqual(liveboard_response.json()[0]["estimated_time"], 180)

    @patch(
        "app.api.metro._taiwan_now",
        return_value=datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    def test_timetable_uses_only_the_current_service_day(self, _mock_now) -> None:
        friday = {
            "Monday": False,
            "Tuesday": False,
            "Wednesday": False,
            "Thursday": False,
            "Friday": True,
            "Saturday": False,
            "Sunday": False,
        }
        saturday = {**friday, "Friday": False, "Saturday": True}
        self.tdx.timetables = [
            {
                "StationID": "BL01",
                "StationName": {"Zh_tw": "頂埔"},
                "Direction": 0,
                "LineID": "BL",
                "DestinationStaionID": "BL23",
                "DestinationStationName": {"Zh_tw": "南港展覽館"},
                "ServiceDay": saturday,
                "Timetables": [{"ArrivalTime": "10:01"}],
            },
            {
                "StationID": "BL01",
                "StationName": {"Zh_tw": "頂埔"},
                "Direction": 0,
                "LineID": "BL",
                "DestinationStationID": "BL23",
                "DestinationStationName": {"Zh_tw": "南港展覽館"},
                "ServiceDay": friday,
                "Timetables": [{"ArrivalTime": "10:05"}],
            },
        ]

        response = self.client.get("/api/v1/metro/TRTC/lines/BL/eta")

        self.assertEqual(response.status_code, 200)
        entries = response.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["destination_id"], "BL23")
        self.assertEqual(entries[0]["estimated_time"], 300)

    @patch(
        "app.api.metro._taiwan_now",
        return_value=datetime(2026, 9, 19, 0, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    def test_timetable_supports_previous_service_day_after_midnight(self, _mock_now) -> None:
        self.tdx.timetables[0]["ServiceDay"] = {"Friday": True}
        self.tdx.timetables[0]["Timetables"] = [
            {"Sequence": 1, "DepartureTime": "23:55"},
            {"Sequence": 2, "DepartureTime": "00:05"},
        ]

        response = self.client.get("/api/v1/metro/TRTC/lines/BL/eta")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entries"][0]["estimated_time"], 300)

    @patch(
        "app.api.metro._taiwan_now",
        return_value=datetime(2026, 9, 18, 23, 58, tzinfo=timezone(timedelta(hours=8))),
    )
    def test_timetable_rolls_current_service_day_past_midnight(self, _mock_now) -> None:
        self.tdx.timetables[0]["ServiceDay"] = {"Friday": True}
        self.tdx.timetables[0]["Timetables"] = [
            {"Sequence": 1, "DepartureTime": "23:55"},
            {"Sequence": 2, "DepartureTime": "00:05"},
        ]

        response = self.client.get("/api/v1/metro/TRTC/lines/BL/eta")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entries"][0]["estimated_time"], 420)


if __name__ == "__main__":
    unittest.main()
