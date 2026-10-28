from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.config import Settings
from app.db import get_connection, init_db
from app.sync_realtime import RealtimeService, RouteBusesService
from app.tdx_client import TDXJSONResponse


class _FakeTDXClient:
    def __init__(self) -> None:
        self.eta_payload_by_route: dict[str, list[dict]] = {}
        self.buses_payload_by_route: dict[str, list[dict]] = {}

    def fetch_estimated_time_of_arrival_batch(
        self,
        city: str,
        routeids: list[str],
        *,
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
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
        payload: list[dict] = []
        for routeid in routeids:
            payload.extend(self.buses_payload_by_route.get(routeid, []))
        return TDXJSONResponse(payload=payload, status_code=200, last_modified=None)

    def fetch_realtime_by_frequency(self, city: str, routeid: str) -> list[dict]:
        return list(self.buses_payload_by_route.get(routeid, []))


class _FakeNtpcOpenDataClient:
    def __init__(self) -> None:
        self.eta_payload_by_route: dict[str, list[dict]] = {}
        self.requested_routeids: list[list[str]] = []

    def fetch_estimated_time_of_arrival_by_subroute(
        self,
        routeids: list[str],
    ) -> dict[str, list[dict]]:
        self.requested_routeids.append(list(routeids))
        return {
            routeid: list(self.eta_payload_by_route.get(routeid, []))
            for routeid in routeids
        }


class RealtimeBackfillTests(unittest.TestCase):
    def _gps_time(self, epoch_seconds: float) -> str:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        self.download_db_path = Path(self.temp_dir.name) / "downloads" / "bus.db"
        self.download_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.app_db_path = Path(self.temp_dir.name) / "app.db"
        init_db(self.db_path)
        self.settings = Settings(
            project_dir=Path(self.temp_dir.name),
            db_path=self.db_path,
            download_db_path=self.download_db_path,
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
            app_db_path=self.app_db_path,
        )
        self.client = _FakeTDXClient()
        self.ntpc_client = _FakeNtpcOpenDataClient()
        self._seed_route("TXG307")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_route(self, routeid: str) -> None:
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO routes (routeid, name, name_en) VALUES (?, ?, ?)",
                    (routeid, "307", "307"),
                )
                connection.execute(
                    "INSERT INTO paths (routeid, pathid, name, name_en) VALUES (?, ?, ?, ?)",
                    (routeid, 0, "Outbound", "Outbound"),
                )
                stops = [
                    (routeid, 0, 1, "STOP1", "Stop 1", "Stop 1", 24.1000, 120.6500),
                    (routeid, 0, 2, "STOP2", "Stop 2", "Stop 2", 24.1006, 120.6500),
                    (routeid, 0, 3, "STOP3", "Stop 3", "Stop 3", 24.1012, 120.6500),
                ]
                connection.executemany(
                    """
                    INSERT INTO stops (routeid, pathid, seq, stopid, name, name_en, lat, lon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    stops,
                )
                path_points = [
                    (routeid, 0, 1, 24.1000, 120.6500),
                    (routeid, 0, 2, 24.1006, 120.6500),
                    (routeid, 0, 3, 24.1012, 120.6500),
                ]
                connection.executemany(
                    "INSERT INTO path_points (routeid, pathid, seq, lat, lon) VALUES (?, ?, ?, ?, ?)",
                    path_points,
                )
                travel_times = [
                    (routeid, 0, 1, 2, 60.0, 4, "eta"),
                    (routeid, 0, 2, 3, 90.0, 4, "eta"),
                ]
                connection.executemany(
                    """
                    INSERT INTO stop_travel_times
                        (routeid, direction, from_seq, to_seq, avg_seconds, sample_count, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    travel_times,
                )

    def _build_services(self) -> tuple[RealtimeService, RouteBusesService]:
        route_buses_service = RouteBusesService(self.settings, self.client)
        realtime_service = RealtimeService(
            self.settings,
            self.client,
            route_buses_service=route_buses_service,
            ntpc_opendata_client=self.ntpc_client,
        )
        return realtime_service, route_buses_service

    def test_waiting_batch_requests_recheck_the_city_cache(self) -> None:
        service, _ = self._build_services()
        city_lock = threading.Lock()
        ready = threading.Barrier(3)

        def wait_for_both_callers(city):
            ready.wait(timeout=5)
            return city_lock

        with patch.object(service, "_get_city_refresh_lock", side_effect=wait_for_both_callers), \
             patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          wraps=self.client.fetch_estimated_time_of_arrival_batch) as fetch, \
             ThreadPoolExecutor(max_workers=2) as executor:
            with city_lock:
                first = executor.submit(service.get_batch_snapshots, ["TXG307"])
                second = executor.submit(service.get_batch_snapshots, ["TXG307"])
                ready.wait(timeout=5)
            self.assertIn("TXG307", first.result(timeout=5))
            self.assertIn("TXG307", second.result(timeout=5))
        self.assertEqual(fetch.call_count, 1)

    def test_eta_reference_and_source_time_survive_cache_and_304(self) -> None:
        source_time = datetime.fromisoformat("2026-10-10T08:00:00+08:00").timestamp()
        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 120, "UpdateTime": "2026-10-10T08:00:00+08:00",
        }]
        service, _ = self._build_services()
        with patch("app.sync_realtime.time.time", return_value=source_time + 35):
            first = service.get_snapshot("TXG307")
        stop = first["paths"][0]["stops"][0]
        self.assertEqual(stop["eta"], 85)
        self.assertEqual(stop["updated_at"], source_time)
        self.assertEqual(stop["eta_reference_at"], source_time + 35)
        with patch("app.sync_realtime.time.time", return_value=source_time + 50), \
             patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          return_value=TDXJSONResponse(None, 304, None)):
            second = service.get_snapshot("TXG307")
        self.assertEqual(second["paths"], first["paths"])
        self.assertEqual(service._cache["TXG307"].fetched_at, source_time + 35)

    def test_repeated_failures_cannot_extend_stale_snapshot_lifetime(self) -> None:
        service, _ = self._build_services()
        with patch("app.sync_realtime.time.time", return_value=1000):
            service.get_snapshot("TXG307")
        with patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          side_effect=RuntimeError("offline")):
            for now in (1010, 1040, 1089):
                with patch("app.sync_realtime.time.time", return_value=now):
                    self.assertTrue(service.get_snapshot("TXG307")["stale"])
                    self.assertIn("TXG307", service.get_batch_snapshots(["TXG307"]))
            with patch("app.sync_realtime.time.time", return_value=1091):
                with self.assertRaises(RuntimeError):
                    service.get_snapshot("TXG307")
                self.assertEqual(service.get_batch_snapshots(["TXG307"]), {})

    def test_prefer_cached_returns_before_slow_refresh_and_coalesces_it(self) -> None:
        now = datetime.now().astimezone().isoformat()
        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 120, "UpdateTime": now,
        }]
        service, _ = self._build_services()
        service.get_snapshot("TXG307")
        service._cache["TXG307"].expires_at = 0
        self.client.eta_payload_by_route["TXG307"][0]["EstimateTime"] = 240

        started = threading.Event()
        release = threading.Event()
        original = self.client.fetch_estimated_time_of_arrival_batch

        def blocked_eta(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError("test refresh was not released")
            return original(*args, **kwargs)

        with patch.object(self.client, "fetch_estimated_time_of_arrival_batch", side_effect=blocked_eta) as fetch, \
             ThreadPoolExecutor(max_workers=1) as executor:
            try:
                preview = executor.submit(service.get_snapshot, "TXG307", prefer_cached=True)
                self.assertTrue(preview.result(timeout=1)["stale"])
                self.assertTrue(started.wait(1))
                self.assertTrue(service.get_snapshot("TXG307", prefer_cached=True)["stale"])
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()

            deadline = time.monotonic() + 3
            while service._background_refreshing_cities and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(service._background_refreshing_cities)
        self.assertGreater(service.get_snapshot("TXG307")["paths"][0]["stops"][0]["eta"], 220)

    def test_prefer_cached_does_not_reuse_old_eta_source(self) -> None:
        old_time = datetime.fromtimestamp(time.time() - 95).astimezone().isoformat()
        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 120, "UpdateTime": old_time,
        }]
        service, _ = self._build_services()
        service.get_snapshot("TXG307")
        service._cache["TXG307"].expires_at = 0
        self.client.eta_payload_by_route["TXG307"][0].update({
            "EstimateTime": 240, "UpdateTime": datetime.now().astimezone().isoformat(),
        })

        with patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          wraps=self.client.fetch_estimated_time_of_arrival_batch) as fetch:
            refreshed = service.get_snapshot("TXG307", prefer_cached=True)
            try:
                fetch.assert_called_once()
                self.assertFalse(refreshed.get("stale", False))
                self.assertGreater(refreshed["paths"][0]["stops"][0]["eta"], 220)
            finally:
                deadline = time.monotonic() + 3
                while service._eta_first_loads and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(service._eta_first_loads)

    def test_prefer_cached_does_not_extend_snapshot_lifetime(self) -> None:
        now = datetime.now().astimezone().isoformat()
        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 120, "UpdateTime": now,
        }]
        service, _ = self._build_services()
        service.get_snapshot("TXG307")
        service._cache["TXG307"].expires_at = 0
        service._cache["TXG307"].fetched_at -= 91
        with patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          wraps=self.client.fetch_estimated_time_of_arrival_batch) as fetch:
            refreshed = service.get_snapshot("TXG307", prefer_cached=True)
            try:
                fetch.assert_called_once()
                self.assertFalse(refreshed.get("stale", False))
            finally:
                deadline = time.monotonic() + 3
                while service._eta_first_loads and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(service._eta_first_loads)

    def test_cold_route_returns_eta_before_deferred_travel_time_write(self) -> None:
        now = datetime.now().astimezone().isoformat()
        self.client.eta_payload_by_route["TXG307"] = [
            {
                "SubRouteUID": "TXG307", "Direction": 0, "StopID": stopid,
                "EstimateTime": eta, "UpdateTime": now, "PlateNumb": "ABC-1234",
            }
            for stopid, eta in (("STOP1", 120), ("STOP2", 180))
        ]
        service, _ = self._build_services()
        scheduled: list = []

        snapshot = service.get_snapshot(
            "TXG307",
            schedule_travel_time_update=scheduled.append,
        )
        self.assertEqual(snapshot["paths"][0]["stops"][0]["eta"], 120)
        self.assertEqual(len(scheduled), 1)
        with get_connection(self.db_path) as connection:
            before = connection.execute(
                "SELECT sample_count FROM stop_travel_times WHERE routeid = ? "
                "AND from_seq = 1 AND to_seq = 2 AND source = 'eta'",
                ("TXG307",),
            ).fetchone()["sample_count"]
        self.assertEqual(before, 4)

        scheduled[0]()
        with get_connection(self.db_path) as connection:
            after = connection.execute(
                "SELECT sample_count FROM stop_travel_times WHERE routeid = ? "
                "AND from_seq = 1 AND to_seq = 2 AND source = 'eta'",
                ("TXG307",),
            ).fetchone()["sample_count"]
        self.assertEqual(after, 5)

    def test_cold_native_eta_precedes_slow_vehicle_fetch_without_duplicate_eta_calls(self) -> None:
        now = datetime.now().astimezone().isoformat()
        self.client.eta_payload_by_route["TXG307"] = [
            {
                "SubRouteUID": "TXG307", "Direction": 0, "StopID": stopid,
                "EstimateTime": eta, "UpdateTime": now, "PlateNumb": "ABC-1234",
            }
            for stopid, eta in (("STOP1", 120), ("STOP2", 180))
        ]
        self.client.buses_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "PlateNumb": "ABC-1234", "Direction": 0,
            "BusPosition": {"PositionLat": 24.1, "PositionLon": 120.65},
        }]
        service, buses_service = self._build_services()
        started = threading.Event()
        release = threading.Event()
        original = self.client.fetch_realtime_by_frequency_batch

        def blocked_buses(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError("test bus source was not released")
            return original(*args, **kwargs)

        with patch.object(self.client, "fetch_realtime_by_frequency_batch", side_effect=blocked_buses), \
             patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          wraps=self.client.fetch_estimated_time_of_arrival_batch) as fetch_eta, \
             ThreadPoolExecutor(max_workers=2) as executor:
            try:
                early = executor.submit(service.get_snapshot, "TXG307", prefer_cached=True)
                preview = early.result(timeout=1)
                self.assertTrue(preview["partial"])
                self.assertEqual(preview["paths"][0]["stops"][0]["eta"], 120)
                self.assertTrue(started.wait(1))
                self.assertEqual(service.get_snapshot("TXG307", prefer_cached=True), preview)
                self.assertEqual(fetch_eta.call_count, 1)
                self.assertNotIn("TXG307", buses_service._cache)
                self.assertNotIn("TXG307", service._last_native_plate_states)
                with get_connection(self.db_path) as connection:
                    count = connection.execute(
                        "SELECT sample_count FROM stop_travel_times WHERE routeid = ? "
                        "AND from_seq = 1 AND to_seq = 2 AND source = 'eta'",
                        ("TXG307",),
                    ).fetchone()["sample_count"]
                self.assertEqual(count, 4)
            finally:
                release.set()

            deadline = time.monotonic() + 3
            while service._eta_first_loads and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(service._eta_first_loads)

        final = service.get_snapshot("TXG307")
        self.assertNotIn("partial", final)
        self.assertIn("TXG307", buses_service._cache)
        self.assertIn("TXG307", service._last_native_plate_states)
        with get_connection(self.db_path) as connection:
            count = connection.execute(
                "SELECT sample_count FROM stop_travel_times WHERE routeid = ? "
                "AND from_seq = 1 AND to_seq = 2 AND source = 'eta'",
                ("TXG307",),
            ).fetchone()["sample_count"]
        self.assertEqual(count, 5)

    def test_eta_first_failure_before_eta_is_retryable(self) -> None:
        service, _ = self._build_services()
        with patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          side_effect=RuntimeError("upstream offline")):
            with self.assertRaisesRegex(RuntimeError, "upstream offline"):
                service.get_snapshot("TXG307", prefer_cached=True)

        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 120, "UpdateTime": datetime.now().astimezone().isoformat(),
        }]
        retried = service.get_snapshot("TXG307", prefer_cached=True)
        self.assertEqual(retried["paths"][0]["stops"][0]["eta"], 120)
        deadline = time.monotonic() + 3
        while service._eta_first_loads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(service._eta_first_loads)

    def test_eta_first_waits_when_source_timestamp_is_missing(self) -> None:
        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 120,
        }]
        service, _ = self._build_services()
        result = service.get_snapshot("TXG307", prefer_cached=True)
        self.assertNotIn("partial", result)
        self.assertEqual(result["paths"][0]["stops"][0]["eta"], 120)

    def test_failed_background_refresh_keeps_original_source_lifetime(self) -> None:
        now = datetime.now().astimezone().isoformat()
        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 120, "UpdateTime": now,
        }]
        service, _ = self._build_services()
        service.get_snapshot("TXG307")
        original_fetched_at = service._cache["TXG307"].fetched_at
        service._cache["TXG307"].expires_at = 0

        with patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          side_effect=RuntimeError("offline")) as fetch:
            self.assertTrue(service.get_snapshot("TXG307", prefer_cached=True)["stale"])
            deadline = time.monotonic() + 3
            while service._background_refreshing_cities and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(service._background_refreshing_cities)
            fetch.assert_called_once()
            self.assertEqual(service._cache["TXG307"].fetched_at, original_fetched_at)
            service._cache["TXG307"].fetched_at -= 91
            with self.assertRaises(RuntimeError):
                service.get_snapshot("TXG307", prefer_cached=True)

    def test_only_opted_in_route_realtime_requests_prefer_cached_data(self) -> None:
        app = FastAPI()
        service = Mock()
        service.get_snapshot.return_value = {"paths": []}
        app.state.realtime_service = service
        app.include_router(routes.router)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/routes/TXG307/realtime").status_code, 200)
            service.get_snapshot.assert_called_with("TXG307")
            self.assertEqual(client.get("/api/v1/routes/TXG307/realtime?prefer_cached=true").status_code, 200)
            service.get_snapshot.assert_called_with(
                "TXG307", prefer_cached=True, schedule_travel_time_update=ANY,
            )

    def test_old_source_data_is_retained_and_flagged_stale(self) -> None:
        source_time = datetime.fromisoformat("2026-10-10T08:00:00+08:00").timestamp()
        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 0, "UpdateTime": "2026-10-10T08:00:00+08:00",
        }]
        service, _ = self._build_services()
        with patch("app.sync_realtime.time.time", return_value=source_time + 91):
            snapshot = service.get_snapshot("TXG307")
        self.assertTrue(snapshot["stale"])
        self.assertEqual(snapshot["paths"][0]["stops"][0]["eta"], -1)


    def test_vehicle_failure_preserves_successful_eta_without_seeding_map_cache(self) -> None:
        source_time = datetime.fromisoformat("2026-10-10T08:00:00+08:00").timestamp()
        self.client.eta_payload_by_route["TXG307"] = [{
            "SubRouteUID": "TXG307", "Direction": 0, "StopID": "STOP1",
            "EstimateTime": 120, "UpdateTime": "2026-10-10T08:00:00+08:00",
        }]
        for mode in ("single", "batch"):
            with self.subTest(mode=mode):
                service, buses = self._build_services()
                with patch("app.sync_realtime.time.time", return_value=source_time + 10), \
                     patch.object(self.client, "fetch_realtime_by_frequency_batch",
                                  side_effect=TimeoutError("GPS feed unavailable")):
                    snapshot = (service.get_snapshot("TXG307") if mode == "single"
                                else service.get_batch_snapshots(["TXG307"])["TXG307"])
                self.assertEqual(snapshot["paths"][0]["stops"][0]["eta"], 110)
                self.assertNotIn("TXG307", buses._cache)

    def test_batch_only_refreshes_routes_still_missing_after_lock(self) -> None:
        self._seed_route("TXG308")
        service, _ = self._build_services()
        snapshot = service.get_snapshot("TXG307")
        service._cache.clear()

        def concurrent_refresh(city):
            service._set_cached("TXG307", snapshot)
            return threading.Lock()

        with patch.object(service, "_get_city_refresh_lock", side_effect=concurrent_refresh), \
             patch.object(self.client, "fetch_estimated_time_of_arrival_batch",
                          wraps=self.client.fetch_estimated_time_of_arrival_batch) as fetch:
            result = service.get_batch_snapshots(["TXG307", "TXG308"])
        self.assertEqual(set(result), {"TXG307", "TXG308"})
        self.assertEqual(fetch.call_args.args[1], ["TXG308"])

    def test_realtime_supplies_map_cache_for_single_batch_and_fallback(self) -> None:
        for mode in ("single", "batch", "fallback"):
            for has_buses in (False, True):
                with self.subTest(mode=mode, has_buses=has_buses):
                    service, buses_service = self._build_services()
                    self.client.buses_payload_by_route["TXG307"] = [{
                        "SubRouteUID": "TXG307", "PlateNumb": "AAA-1234",
                        "Direction": 0, "BusPosition": {"PositionLat": 24.1, "PositionLon": 120.65},
                    }] if has_buses else []
                    if mode == "single":
                        service.get_snapshot("TXG307")
                    elif mode == "batch":
                        service.get_batch_snapshots(["TXG307"])
                    else:
                        service._get_single_route_snapshot(
                            "TXG307", service._load_static_route("TXG307"), force_refresh=False,
                        )
                    with patch.object(self.client, "fetch_realtime_by_frequency_batch") as fetch:
                        buses = buses_service.get_buses("TXG307")
                    fetch.assert_not_called()
                    self.assertEqual(len(buses), int(has_buses))

    def test_eta_304_and_failure_do_not_extend_vehicle_cache(self) -> None:
        service, buses_service = self._build_services()
        service.get_snapshot("TXG307")
        original_expiry = buses_service._cache["TXG307"].expires_at
        for response in (TDXJSONResponse([], 304, None), RuntimeError("offline")):
            with self.subTest(response=type(response).__name__):
                service._cache["TXG307"].expires_at = 0
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch.object(self.client, "fetch_estimated_time_of_arrival_batch", **kwargs), \
                     patch.object(self.client, "fetch_realtime_by_frequency_batch") as fetch:
                    service.get_snapshot("TXG307")
                fetch.assert_not_called()
                self.assertEqual(buses_service._cache["TXG307"].expires_at, original_expiry)

    def test_uses_ntpc_eta_fallback_when_new_taipei_tdx_eta_is_empty(self) -> None:
        routeid = "NWT157491"
        self._seed_route(routeid)
        self.client.eta_payload_by_route[routeid] = []
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "NTPC-ETA",
                "BusPosition": {"PositionLat": 25.0, "PositionLon": 121.0},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]
        self.ntpc_client.eta_payload_by_route[routeid] = [
            {
                "routeid": "16468",
                "stopid": "STOP2",
                "estimatetime": "45",
                "goback": "0",
            },
            {
                "routeid": "16468",
                "stopid": "STOP3",
                "estimatetime": "-1",
                "goback": "0",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = next(path for path in snapshot["paths"] if path["pathid"] == 0)
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        self.assertEqual(stops["STOP2"]["eta"], 45)
        self.assertEqual(
            stops["STOP2"]["etas"],
            [
                {
                    "plate": None,
                    "eta": 45,
                    "is_arriving": False,
                    "source": "ntpc_opendata",
                    "estimated": False,
                }
            ],
        )
        self.assertNotIn("STOP3", stops)

    def test_eta_first_waits_for_vehicle_dependent_ntpc_fallback(self) -> None:
        routeid = "NWT157491"
        self._seed_route(routeid)
        self.client.buses_payload_by_route[routeid] = [{
            "RouteUID": routeid,
            "SubRouteUID": routeid,
            "Direction": 0,
            "PlateNumb": "NTPC-ETA",
            "BusPosition": {"PositionLat": 25.0, "PositionLon": 121.0},
        }]
        self.ntpc_client.eta_payload_by_route[routeid] = [{
            "routeid": "16468", "stopid": "STOP2", "estimatetime": "45", "goback": "0",
        }]
        service, _ = self._build_services()

        result = service.get_snapshot(routeid, prefer_cached=True)

        self.assertNotIn("partial", result)
        self.assertEqual(result["paths"][0]["stops"][0]["eta"], 45)

    def test_skips_ntpc_eta_fallback_when_tdx_has_no_realtime_buses(self) -> None:
        routeid = "NWT157491"
        self._seed_route(routeid)
        self.client.eta_payload_by_route[routeid] = []
        self.client.buses_payload_by_route[routeid] = []
        self.ntpc_client.eta_payload_by_route[routeid] = [
            {
                "routeid": "16468",
                "stopid": "STOP2",
                "estimatetime": "0",
                "goback": "0",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = next(path for path in snapshot["paths"] if path["pathid"] == 0)
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        self.assertNotIn("STOP2", stops)
        self.assertEqual(self.ntpc_client.requested_routeids, [])

    def test_ntpc_eta_fallback_does_not_override_native_tdx_eta(self) -> None:
        routeid = "NWT157491"
        self._seed_route(routeid)
        self.client.eta_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "StopID": "STOP1",
                "EstimateTime": 120,
                "UpdateTime": "2026-06-22T10:00:00+08:00",
            }
        ]
        self.ntpc_client.eta_payload_by_route[routeid] = [
            {
                "routeid": "16468",
                "stopid": "STOP2",
                "estimatetime": "45",
                "goback": "0",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = next(path for path in snapshot["paths"] if path["pathid"] == 0)
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        self.assertIn("STOP1", stops)
        self.assertNotIn("STOP2", stops)
        self.assertEqual(self.ntpc_client.requested_routeids, [])

    def test_does_not_backfill_on_first_seen_bus_only_snapshot(self) -> None:
        routeid = "TXG307"
        self.client.eta_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "StopID": "STOP1",
                "EstimateTime": 120,
                "UpdateTime": "2026-06-22T10:00:00+08:00",
                "PlateNumb": "AAA-0001",
                "VehicleStopStatus": 0,
            }
        ]
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "AAA-0001",
                "BusPosition": {"PositionLat": 24.1000, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:00+08:00",
            },
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "BBB-0002",
                "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        self.assertNotIn("STOP2", stops)
        self.assertNotIn("STOP3", stops)

    def test_backfills_missing_bus_into_buses_and_etas_after_eta_disappears(self) -> None:
        routeid = "TXG307"
        realtime_service, _ = self._build_services()
        base_time = 1_700_000_000.0
        with patch("app.sync_realtime.time.time", return_value=base_time):
            self.client.eta_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "StopID": "STOP2",
                    "EstimateTime": 30,
                    "UpdateTime": "2026-06-22T10:00:00+08:00",
                    "PlateNumb": "BBB-0002",
                    "VehicleStopStatus": 1,
                }
            ]
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "BBB-0002",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time - 5),
                },
            ]
            realtime_service.get_snapshot(routeid, force_refresh=True)

        with patch("app.sync_realtime.time.time", return_value=base_time + 10):
            self.client.eta_payload_by_route[routeid] = []
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "BBB-0002",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time + 5),
                },
            ]
            snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        self.assertIn("STOP2", stops)
        self.assertIn("STOP3", stops)
        anchor_stop = stops["STOP2"]
        downstream_stop = stops["STOP3"]

        self.assertEqual(
            anchor_stop["buses"],
            [{"id": "BBB-0002", "type": "normal", "source": "backfill_buses"}],
        )
        self.assertIn(
            {
                "plate": "BBB-0002",
                "eta": 0,
                "is_arriving": True,
                "source": "backfill_buses",
                "estimated": True,
            },
            anchor_stop["etas"],
        )
        self.assertEqual(anchor_stop["eta"], 0)
        self.assertIn(
            {
                "plate": "BBB-0002",
                "eta": 90,
                "is_arriving": False,
                "source": "backfill_buses",
                "estimated": True,
            },
            downstream_stop["etas"],
        )

    def test_does_not_backfill_from_gps_without_previous_native_eta_plate(self) -> None:
        routeid = "TXG307"
        base_time = 1_700_000_000.0
        self.client.eta_payload_by_route[routeid] = []
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "ZZZ-9999",
                "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                "GPSTime": self._gps_time(base_time - 5),
            },
        ]

        realtime_service, _ = self._build_services()
        with patch("app.sync_realtime.time.time", return_value=base_time):
            snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        self.assertEqual(path["stops"], [])

    def test_does_not_backfill_multiple_gps_only_buses_on_same_path(self) -> None:
        routeid = "TXG307"
        base_time = 1_700_000_000.0
        self.client.eta_payload_by_route[routeid] = []
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "GPS-0001",
                "BusPosition": {"PositionLat": 24.1000, "PositionLon": 120.6500},
                "GPSTime": self._gps_time(base_time - 5),
            },
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "GPS-0002",
                "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                "GPSTime": self._gps_time(base_time - 5),
            },
        ]

        realtime_service, _ = self._build_services()
        with patch("app.sync_realtime.time.time", return_value=base_time):
            snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        self.assertEqual(path["stops"], [])

    def test_backfill_requires_eta_to_disappear_from_previous_snapshot(self) -> None:
        routeid = "TXG307"
        realtime_service, _ = self._build_services()
        base_time = 1_700_000_000.0

        with patch("app.sync_realtime.time.time", return_value=base_time):
            self.client.eta_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "StopID": "STOP2",
                    "EstimateTime": 45,
                    "UpdateTime": "2026-06-22T10:00:00+08:00",
                    "PlateNumb": "BBB-0002",
                    "VehicleStopStatus": 0,
                }
            ]
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "BBB-0002",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time - 5),
                },
            ]
            realtime_service.get_snapshot(routeid, force_refresh=True)

        with patch("app.sync_realtime.time.time", return_value=base_time + 10):
            self.client.eta_payload_by_route[routeid] = []
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "BBB-0002",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time + 5),
                },
            ]
            snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        self.assertIn("STOP2", stops)
        stop2 = stops["STOP2"]
        self.assertEqual(stop2["buses"], [{"id": "BBB-0002", "type": "normal", "source": "backfill_buses"}])

    def test_backfill_expires_after_disappearance_window(self) -> None:
        routeid = "TXG307"
        realtime_service, _ = self._build_services()
        base_time = 1_700_000_000.0

        with patch("app.sync_realtime.time.time", return_value=base_time):
            self.client.eta_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "StopID": "STOP2",
                    "EstimateTime": 45,
                    "UpdateTime": "2026-06-22T10:00:00+08:00",
                    "PlateNumb": "BBB-0002",
                    "VehicleStopStatus": 0,
                }
            ]
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "BBB-0002",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time - 5),
                },
            ]
            realtime_service.get_snapshot(routeid, force_refresh=True)

        with patch("app.sync_realtime.time.time", return_value=base_time + 200.0):
            self.client.eta_payload_by_route[routeid] = []
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "BBB-0002",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time + 195),
                },
            ]
            snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        for stop in path["stops"]:
            self.assertEqual(stop["buses"], [])
            self.assertEqual(stop["etas"], [])

    def test_does_not_backfill_vehicle_last_seen_at_terminal_stop(self) -> None:
        routeid = "TXG307"
        realtime_service, _ = self._build_services()

        self.client.eta_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "StopID": "STOP3",
                "EstimateTime": 10,
                "UpdateTime": "2026-06-22T10:00:00+08:00",
                "PlateNumb": "TER-0001",
                "VehicleStopStatus": 1,
            }
        ]
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "TER-0001",
                "BusPosition": {"PositionLat": 24.1012, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]
        realtime_service.get_snapshot(routeid, force_refresh=True)

        self.client.eta_payload_by_route[routeid] = []
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        for stop in path["stops"]:
            self.assertEqual(stop["buses"], [])
            self.assertEqual(stop["etas"], [])

    @patch("app.sync_realtime.time.time", return_value=datetime.fromisoformat("2026-06-22T10:00:05+08:00").timestamp())
    def test_does_not_duplicate_native_plate(self, _clock) -> None:
        routeid = "TXG307"
        self.client.eta_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "StopID": "STOP2",
                "EstimateTime": 30,
                "UpdateTime": "2026-06-22T10:00:00+08:00",
                "PlateNumb": "CCC-0003",
                "VehicleStopStatus": 1,
            }
        ]
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "CCC-0003",
                "BusPosition": {"PositionLat": 24.1006, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        stop2 = next(stop for stop in path["stops"] if stop["stopid"] == "STOP2")
        self.assertEqual(stop2["buses"], [{"id": "CCC-0003", "type": "normal", "source": "tdx"}])
        self.assertEqual(len([eta for eta in stop2["etas"] if eta["plate"] == "CCC-0003"]), 1)

    def test_backfills_future_stops_when_travel_time_missing(self) -> None:
        routeid = "TXG307"
        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM stop_travel_times WHERE routeid = ?",
                    (routeid,),
                )

        realtime_service, _ = self._build_services()
        self.client.eta_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "StopID": "STOP2",
                "EstimateTime": 20,
                "UpdateTime": "2026-06-22T10:00:00+08:00",
                "PlateNumb": "DDD-0004",
                "VehicleStopStatus": 1,
            }
        ]
        base_time = 1_700_000_000.0
        with patch("app.sync_realtime.time.time", return_value=base_time):
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "DDD-0004",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time - 5),
                },
            ]
            realtime_service.get_snapshot(routeid, force_refresh=True)

        with patch("app.sync_realtime.time.time", return_value=base_time + 10):
            self.client.eta_payload_by_route[routeid] = []
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "DDD-0004",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time + 5),
                },
            ]
            snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)
        path = snapshot["paths"][0]
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        self.assertIn("STOP2", stops)
        stop2 = stops["STOP2"]

        self.assertEqual(stop2["buses"], [{"id": "DDD-0004", "type": "normal", "source": "backfill_buses"}])
        self.assertEqual(
            stop2["etas"],
            [
                {
                    "plate": "DDD-0004",
                    "eta": 0,
                    "is_arriving": True,
                    "source": "backfill_buses",
                    "estimated": True,
                }
            ],
        )
        self.assertEqual(stop2["eta"], 0)
        self.assertIn("STOP3", stops)
        stop3 = stops["STOP3"]
        self.assertEqual(
            stop3["buses"],
            [],
        )
        self.assertEqual(len(stop3["etas"]), 1)
        self.assertEqual(stop3["etas"][0]["plate"], "DDD-0004")
        self.assertEqual(stop3["etas"][0]["source"], "backfill_buses")
        self.assertTrue(stop3["etas"][0]["estimated"])
        self.assertGreater(stop3["etas"][0]["eta"], 0)
        self.assertEqual(stop3["eta"], stop3["etas"][0]["eta"])

    def test_backfills_future_stops_when_travel_time_table_missing(self) -> None:
        routeid = "TXG307"
        realtime_service, _ = self._build_services()
        base_time = 1_700_000_000.0
        with patch("app.sync_realtime.time.time", return_value=base_time):
            self.client.eta_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "StopID": "STOP2",
                    "EstimateTime": 20,
                    "UpdateTime": "2026-06-22T10:00:00+08:00",
                    "PlateNumb": "NO-TABLE",
                    "VehicleStopStatus": 1,
                }
            ]
            self.client.buses_payload_by_route[routeid] = [
                {
                    "RouteUID": routeid,
                    "SubRouteUID": routeid,
                    "Direction": 0,
                    "PlateNumb": "NO-TABLE",
                    "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                    "GPSTime": self._gps_time(base_time - 5),
                },
            ]
            realtime_service.get_snapshot(routeid, force_refresh=True)

        with get_connection(self.db_path) as connection:
            with connection:
                connection.execute("DROP TABLE stop_travel_times")

        self.client.eta_payload_by_route[routeid] = []
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "NO-TABLE",
                "BusPosition": {"PositionLat": 24.10062, "PositionLon": 120.6500},
                "GPSTime": self._gps_time(base_time - 5),
            },
        ]

        with patch("app.sync_realtime.time.time", return_value=base_time + 10):
            snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)

        path = snapshot["paths"][0]
        stops = {stop["stopid"]: stop for stop in path["stops"]}
        self.assertEqual(
            stops["STOP2"]["buses"],
            [{"id": "NO-TABLE", "type": "normal", "source": "backfill_buses"}],
        )
        self.assertIn("STOP3", stops)
        self.assertGreater(stops["STOP3"]["eta"], 0)

    def test_skips_bus_that_is_too_far_from_any_stop(self) -> None:
        routeid = "TXG307"
        self.client.eta_payload_by_route[routeid] = []
        self.client.buses_payload_by_route[routeid] = [
            {
                "RouteUID": routeid,
                "SubRouteUID": routeid,
                "Direction": 0,
                "PlateNumb": "EEE-0005",
                "BusPosition": {"PositionLat": 24.1100, "PositionLon": 120.6500},
                "GPSTime": "2026-06-22T10:00:05+08:00",
            },
        ]

        realtime_service, _ = self._build_services()
        snapshot = realtime_service.get_snapshot(routeid, force_refresh=True)
        path = snapshot["paths"][0]

        for stop in path["stops"]:
            self.assertEqual(stop["buses"], [])
            self.assertEqual(stop["etas"], [])
            self.assertIsNone(stop["eta"])


if __name__ == "__main__":
    unittest.main()
