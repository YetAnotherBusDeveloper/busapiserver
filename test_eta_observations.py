from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import test_admin_api as admin_fixture
import test_sync_realtime as realtime_fixture
from app.api import analytics as analytics_api
from app.auth_service import OAuthIdentity, _upsert_login
from app.config import Settings
from app.db import get_connection, init_app_db, init_db
from app.eta_observations import (
    EtaObservationCollector,
    EtaObservationConfig,
    EtaObservationStore,
    build_collector,
    classify_error,
    prepare_snapshot,
)
from app.rate_limit import reset_rate_limit_state
from app.sync_realtime import RealtimeService, RouteBusesService
from app.tdx_client import TDXClient, TDXJSONResponse

REPO = Path(__file__).resolve().parent
T0 = 1_795_000_000  # a fixed "upstream time" well away from now


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def row(seq, *, eta=None, plate="AAA-001", status=0, src=T0, estimates=None, next_bus=None,
        sub="TXG307", direction=0, extra=None):
    item = {
        "SubRouteUID": sub, "RouteUID": sub, "Direction": direction, "StopSequence": seq,
        "StopUID": f"TXG{seq:04d}", "StopStatus": status, "PlateNumb": plate if eta is not None else "",
        "SrcUpdateTime": iso(src), "UpdateTime": iso(src + 19),
    }
    if eta is not None:
        item["EstimateTime"] = eta
        item["Estimates"] = estimates if estimates is not None else [
            {"PlateNumb": plate, "EstimateTime": eta, "IsLastBus": False}
        ]
    if next_bus is not None:
        item["NextBusTime"] = iso(next_bus)
    item.update(extra or {})
    return item


def route_rows(src=T0, *, stops=6, eta_base=120):
    """A route with predictions on the first half and waiting stops after."""
    half = stops // 2
    return [row(seq, eta=eta_base + 60 * seq, src=src) for seq in range(1, half + 1)] + [
        row(seq, status=1, src=src, next_bus=T0 + 3600) for seq in range(half + 1, stops + 1)
    ]


def query(path: Path, sql: str, args=()):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.temp_dir.name)
        self.db = self.dir / "eta_observations.db"
        self._collectors: list[EtaObservationCollector] = []

    def tearDown(self) -> None:
        for collector in self._collectors:
            collector.close()
        self.temp_dir.cleanup()

    def config(self, **overrides) -> EtaObservationConfig:
        values = dict(
            db_path=self.db, cities=frozenset({"Taichung"}), min_free_bytes=0,
            flush_interval=0.02, maintenance_interval=3600.0,
        )
        values.update(overrides)
        return EtaObservationConfig(**values)

    def collector(self, **kwargs) -> EtaObservationCollector:
        hooks = {key: kwargs.pop(key) for key in ("clock", "free_bytes") if key in kwargs}
        collector = EtaObservationCollector(self.config(**kwargs), **hooks)
        self._collectors.append(collector)
        return collector

    def prepared(self, items, *, city="Taichung", routeid="TXG307", truncated=False, observed_at=T0 + 25):
        return prepare_snapshot(city, routeid, items, observed_at=observed_at, truncated=truncated)


class PrepareSnapshotTests(unittest.TestCase):
    def test_keeps_raw_values_for_every_bus_at_a_stop(self) -> None:
        items = [
            row(4, eta=180, estimates=[
                {"PlateNumb": "AAA-001", "EstimateTime": 180, "IsLastBus": False, "VehicleStopStatus": 1},
                {"PlateNumb": "BBB-002", "EstimateTime": 900, "IsLastBus": True},
            ]),
        ]
        snapshot = prepare_snapshot("Taichung", "TXG307", items, observed_at=T0 + 25, truncated=False)
        self.assertEqual(
            snapshot.predictions,
            [
                ("TXG307", 0, 4, 0, "TXG0004", "AAA-001", 180, 0, 1, 0, None),
                ("TXG307", 0, 4, 1, "TXG0004", "BBB-002", 900, 0, None, 1, None),
            ],
        )
        self.assertEqual((snapshot.src_update_at, snapshot.tdx_update_at, snapshot.observed_at), (T0, T0 + 19, T0 + 25))

    def test_missing_plate_is_null_not_guessed(self) -> None:
        for raw in ("", "-1", None, "   "):
            items = [row(1, eta=60, estimates=[{"PlateNumb": raw, "EstimateTime": 60}])]
            snapshot = prepare_snapshot("Taichung", "TXG307", items, observed_at=T0, truncated=False)
            self.assertIsNone(snapshot.predictions[0][5])
            self.assertIsNone(snapshot.predictions[0][9])  # IsLastBus absent stays unknown

    def test_stop_without_prediction_becomes_a_state_row(self) -> None:
        snapshot = prepare_snapshot(
            "Taichung", "TXG307", [row(2, status=1, next_bus=T0 + 600), row(3, status=3)],
            observed_at=T0, truncated=False,
        )
        self.assertEqual(snapshot.predictions, [])
        self.assertEqual(
            snapshot.states,
            [("TXG307", 0, 2, T0, "TXG0002", 1, T0 + 600), ("TXG307", 0, 3, T0, "TXG0003", 3, None)],
        )

    def test_top_level_estimate_without_estimates_list(self) -> None:
        item = row(1, eta=240)
        del item["Estimates"]
        snapshot = prepare_snapshot("Taichung", "TXG307", [item], observed_at=T0, truncated=False)
        self.assertEqual(snapshot.predictions[0][3:7], (0, "TXG0001", "AAA-001", 240))

    def test_rows_with_older_source_time_keep_their_lag(self) -> None:
        snapshot = prepare_snapshot(
            "InterCity", "THB1", [row(1, eta=60, src=T0), row(2, eta=120, src=T0 - 40)],
            observed_at=T0, truncated=False,
        )
        self.assertEqual((snapshot.src_update_at, snapshot.src_update_min_at), (T0, T0 - 40))
        self.assertEqual([p[10] for p in snapshot.predictions], [None, 40])

    def test_malformed_rows_are_skipped_and_no_source_time_means_no_snapshot(self) -> None:
        broken = [{"StopSequence": 1}, "nonsense", row(2, eta=60, extra={"StopSequence": None})]
        snapshot = prepare_snapshot("Taichung", "TXG307", broken + [row(3, eta=60)], observed_at=T0, truncated=False)
        self.assertEqual([p[2] for p in snapshot.predictions], [3])
        timeless = [{"SubRouteUID": "TXG307", "StopUID": "S", "StopSequence": 1, "EstimateTime": 60}]
        self.assertIsNone(prepare_snapshot("Taichung", "TXG307", timeless, observed_at=T0, truncated=False))


class StoreTests(_TempCase):
    def store(self, **overrides) -> EtaObservationStore:
        hooks = {key: overrides.pop(key) for key in ("clock", "free_bytes") if key in overrides}
        store = EtaObservationStore(self.config(**overrides), **hooks)
        self.addCleanup(store.close)
        return store

    def test_new_file_uses_wal_and_can_return_space(self) -> None:
        store = self.store()
        store.write([self.prepared(route_rows())])
        connection = store.open()
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        self.assertTrue(store.auto_vacuum)

    def test_schema_has_no_place_for_a_person(self) -> None:
        self.store().write([self.prepared(route_rows())])
        columns = {
            name.lower()
            for table in ("eta_snapshots", "eta_predictions", "eta_stop_states")
            for _, name, *_ in query(self.db, f"PRAGMA table_info({table})")
        }
        for banned in ("account", "user", "device", "ip", "token", "agent", "session", "principal"):
            self.assertFalse([c for c in columns if banned in c.split("_")], banned)

    def test_same_snapshot_is_stored_once(self) -> None:
        store = self.store()
        first = store.write([self.prepared(route_rows())])
        second = store.write([self.prepared(route_rows(), observed_at=T0 + 60)])
        self.assertEqual((first["snapshots"], second["snapshots"], second["duplicates"]), (1, 0, 1))
        self.assertEqual(query(self.db, "SELECT COUNT(*), MIN(observed_at) FROM eta_snapshots"), [(1, T0 + 25)])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_predictions"), [(3,)])

    def test_city_is_part_of_the_snapshot_identity(self) -> None:
        store = self.store()
        store.write([
            self.prepared(route_rows(), city="Taichung", routeid="R1"),
            self.prepared(route_rows(), city="Kaohsiung", routeid="R1"),
        ])
        self.assertEqual(query(self.db, "SELECT city FROM eta_snapshots ORDER BY city"), [("Kaohsiung",), ("Taichung",)])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_stop_states"), [(6,)])

    def test_fuller_copy_replaces_a_truncated_one_but_not_the_reverse(self) -> None:
        store = self.store()
        store.write([self.prepared(route_rows(stops=6)[:2], truncated=True)])
        self.assertEqual(query(self.db, "SELECT stop_rows, eta_rows, truncated FROM eta_snapshots"), [(2, 2, 1)])
        counts = store.write([self.prepared(route_rows(stops=6))])
        self.assertEqual(counts["replaced"], 1)
        self.assertEqual(query(self.db, "SELECT stop_rows, eta_rows, truncated FROM eta_snapshots"), [(6, 3, 0)])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_predictions"), [(3,)])
        store.write([self.prepared(route_rows(stops=6)[:1], truncated=True)])
        self.assertEqual(query(self.db, "SELECT stop_rows, truncated FROM eta_snapshots"), [(6, 0)])

    def test_stop_state_written_only_when_status_or_next_bus_changes(self) -> None:
        store = self.store()

        def write(src, **state):
            return store.write([self.prepared([row(5, src=src, **state)])])["states"]

        self.assertEqual(write(T0, status=1, next_bus=T0 + 600), 1)
        self.assertEqual(write(T0 + 30, status=1, next_bus=T0 + 600), 0)
        self.assertEqual(write(T0 + 60, status=1, next_bus=T0 + 1200), 1)  # next bus moved
        self.assertEqual(write(T0 + 90, status=3), 1)                       # status changed
        self.assertEqual(
            query(self.db, "SELECT src_update_at, stop_status, next_bus_at FROM eta_stop_states ORDER BY 1"),
            [(T0, 1, T0 + 600), (T0 + 60, 1, T0 + 1200), (T0 + 90, 3, None)],
        )

    def test_state_is_recorded_again_after_a_prediction_in_between(self) -> None:
        store = self.store()
        store.write([self.prepared([row(5, src=T0, status=1, next_bus=T0 + 600)])])
        store.write([self.prepared([row(5, src=T0 + 30, eta=120)])])
        counts = store.write([self.prepared([row(5, src=T0 + 60, status=1, next_bus=T0 + 600)])])
        self.assertEqual(counts["states"], 1)

    def test_restart_does_not_rewrite_unchanged_states(self) -> None:
        first = self.store()
        first.write([self.prepared(route_rows(T0))])
        first.close()
        second = self.store()
        counts = second.write([self.prepared(route_rows(T0 + 30))])
        self.assertEqual((counts["snapshots"], counts["states"]), (1, 0))

    def test_failed_write_leaves_nothing_behind(self) -> None:
        store = self.store()
        good = self.prepared(route_rows(T0))
        bad = replace(self.prepared(route_rows(T0 + 30)), predictions=[("only", "three", "values")])
        with self.assertRaises(sqlite3.Error):
            store.write([good, bad])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(0,)])
        # ... and the state cache was not advanced by the rolled-back batch.
        self.assertEqual(store.write([good])["states"], 3)

    def test_retention_drops_old_rows_and_folds_the_wal(self) -> None:
        now = T0 + 40 * 86400
        store = self.store(clock=lambda: now)
        old = [self.prepared(route_rows(T0 + i * 30), routeid=f"R{i}") for i in range(50)]
        fresh = self.prepared(route_rows(now - 60), routeid="FRESH")
        store.write(old + [fresh])
        result = store.maintain()
        self.assertEqual(result["expired"], 50)
        self.assertEqual(query(self.db, "SELECT routeid FROM eta_snapshots"), [("FRESH",)])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_predictions"), [(3,)])
        # The waiting stops never changed state, so their single (old) row is
        # still the current state and must survive retention.
        self.assertEqual(query(self.db, "SELECT COUNT(*), MAX(src_update_at) FROM eta_stop_states"), [(3, T0)])
        store.write([self.prepared([row(4, status=1, src=T0 + 60, next_bus=T0 + 7200)])])
        store.write([self.prepared([row(4, status=3, src=T0 + 90)])])
        store.maintain()
        self.assertEqual(
            query(self.db, "SELECT src_update_at FROM eta_stop_states WHERE stop_seq = 4"), [(T0 + 90,)]
        )
        wal = Path(str(self.db) + "-wal")
        self.assertLessEqual(wal.stat().st_size if wal.exists() else 0, 0)

    def test_size_budget_counts_wal_and_evicts_oldest_first(self) -> None:
        budget = 400_000
        store = self.store(max_bytes=budget, clock=lambda: T0 + 86400)
        for hour in range(24):
            store.write([
                self.prepared(route_rows(T0 + hour * 3600 + i * 30, stops=60), routeid=f"R{i}")
                for i in range(20)
            ])
        self.assertGreater(store.disk_bytes(), budget)
        result = store.maintain(budget_seconds=20)
        self.assertGreater(result["evicted"], 0)
        self.assertLessEqual(store.disk_bytes(), budget)
        newest, oldest = query(self.db, "SELECT MAX(src_update_at), MIN(src_update_at) FROM eta_snapshots")[0]
        self.assertGreaterEqual(newest, T0 + 23 * 3600)
        self.assertGreater(oldest, T0)
        self.assertEqual(query(self.db, "PRAGMA integrity_check"), [("ok",)])

    def test_disk_bytes_includes_wal_and_shm(self) -> None:
        store = self.store()
        store.write([self.prepared(route_rows(stops=200))])
        parts = sum(Path(str(self.db) + suffix).stat().st_size for suffix in ("", "-wal", "-shm"))
        self.assertGreater(Path(str(self.db) + "-wal").stat().st_size, 0)
        self.assertEqual(store.disk_bytes(), parts)


class CollectorTests(_TempCase):
    def test_disabled_means_no_collector_no_file_no_thread(self) -> None:
        before = threading.active_count()
        settings = SimpleNamespace(eta_observations_enabled=False, eta_observations_db_path=self.db)
        self.assertIsNone(build_collector(settings))
        self.assertFalse(self.db.exists())
        self.assertEqual(threading.active_count(), before)

    def test_settings_map_onto_the_collector(self) -> None:
        settings = SimpleNamespace(
            eta_observations_enabled=True, eta_observations_db_path=self.db,
            eta_observations_cities=("Taichung", "Tainan"), eta_observations_retention_days=7,
            eta_observations_max_bytes=123, eta_observations_min_free_bytes=456,
            eta_observations_quarantine_max_bytes=789,
        )
        config = build_collector(settings).config
        self.assertEqual(
            (config.cities, config.retention_seconds, config.max_bytes, config.min_free_bytes,
             config.quarantine_max_bytes),
            (frozenset({"Taichung", "Tainan"}), 7 * 86400, 123, 456, 789),
        )
        self.assertFalse(self.db.exists())  # nothing is created until start()

    def test_quarantine_budget_comes_from_the_environment(self) -> None:
        for extra, expected in (({}, 1024**3), ({"ETA_OBSERVATIONS_QUARANTINE_MAX_BYTES": "5000"}, 5000)):
            with patch.dict(os.environ, {"ETA_OBSERVATIONS_ENABLED": "true", **extra}, clear=True), patch(
                "app.config.load_dotenv"
            ):
                settings = Settings.from_env()
            self.assertEqual(build_collector(settings).config.quarantine_max_bytes, expected)

    def test_status_names_the_worker_it_came_from(self) -> None:
        status = self.collector().status()
        self.assertEqual((status["worker_pid"], status["counters_scope"]), (os.getpid(), "single_worker"))

    def test_each_worker_reports_only_its_own_counters(self) -> None:
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "import test_eta_observations as t\n"
            "from app.eta_observations import EtaObservationCollector, EtaObservationConfig\n"
            "collector = EtaObservationCollector(EtaObservationConfig(\n"
            "    db_path=Path(sys.argv[1]), cities=frozenset({'Taichung'}), min_free_bytes=0, flush_interval=0.02))\n"
            "collector.start()\n"
            "for i in range(int(sys.argv[3])):\n"
            "    assert collector.submit('Taichung', sys.argv[2] + str(i), t.route_rows())\n"
            "assert collector.close(60)\n"
            "print(json.dumps(collector.status()))\n"
        )
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.db), prefix, str(count)], cwd=REPO,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for prefix, count in (("A", 3), ("B", 5))
        ]
        reports = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=120)
            self.assertEqual(worker.returncode, 0, stderr.decode(errors="replace")[-600:])
            reports.append(json.loads(stdout.decode().strip().splitlines()[-1]))
        pids = [report["worker_pid"] for report in reports]
        self.assertEqual(len({os.getpid(), *pids}), 3)  # two workers, neither of them this process
        # Both wrote to one file, yet each answer covers its own worker only.
        self.assertEqual([report["snapshots_written"] for report in reports], [3, 5])
        self.assertEqual([report["counters_scope"] for report in reports], ["single_worker"] * 2)
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(8,)])

    def test_writes_in_background_and_reports_counters(self) -> None:
        collector = self.collector()
        collector.start()
        for i in range(10):
            self.assertTrue(collector.submit("Taichung", f"R{i}", route_rows()))
        self.assertTrue(collector.close())
        status = collector.status()
        self.assertEqual((status["submitted"], status["snapshots_written"], status["predictions_written"]), (10, 10, 30))
        self.assertEqual(status["stop_states_written"], 3)  # same subroute/stops in every test route
        self.assertEqual((status["write_failures"], status["dropped_queue_full"], status["state"]), (0, 0, "stopped"))
        self.assertIsNotNone(status["last_write_at"])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(10,)])

    def test_only_configured_cities_are_kept(self) -> None:
        collector = self.collector()
        self.assertFalse(collector.submit("Taipei", "TPE307", route_rows()))
        self.assertFalse(collector.submit(None, "X", route_rows()))
        self.assertFalse(collector.submit("Taichung", "TXG307", []))
        self.assertEqual(collector.status()["submitted"], 0)

    def test_starting_twice_runs_one_writer(self) -> None:
        collector = self.collector()
        collector.start()
        collector.start()
        writers = [t for t in threading.enumerate() if t.name == "eta-observations"]
        self.assertEqual(len(writers), 1)
        self.assertTrue(collector.close())
        self.assertTrue(collector.close())  # closing twice is harmless too

    def test_full_queue_drops_and_counts_instead_of_blocking(self) -> None:
        collector = self.collector(queue_capacity=3)  # never started: nothing drains it
        started = time.perf_counter()
        results = [collector.submit("Taichung", f"R{i}", route_rows()) for i in range(10)]
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(results.count(True), 3)
        status = collector.status()
        self.assertEqual((status["dropped_queue_full"], status["queued"], status["queue_capacity"]), (7, 3, 3))

    def test_submit_copies_the_list_it_is_given(self) -> None:
        collector = self.collector()
        items = route_rows()
        collector.submit("Taichung", "TXG307", items)
        items.clear()  # what the realtime service does afterwards must not matter
        collector.start()
        collector.close()
        self.assertEqual(query(self.db, "SELECT stop_rows FROM eta_snapshots"), [(6,)])

    def test_restart_continues_the_same_file(self) -> None:
        first = self.collector()
        first.start()
        first.submit("Taichung", "TXG307", route_rows(T0))
        first.close()
        second = self.collector()
        second.start()
        second.submit("Taichung", "TXG307", route_rows(T0))        # replayed after restart
        second.submit("Taichung", "TXG307", route_rows(T0 + 30))
        second.close()
        status = second.status()
        self.assertEqual((status["snapshots_written"], status["snapshots_duplicate"], status["stop_states_written"]), (1, 1, 0))
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(2,)])

    def test_two_writers_on_one_file_store_each_snapshot_once(self) -> None:
        collectors = [self.collector(), self.collector()]
        for collector in collectors:
            collector.start()
        for i in range(40):
            for collector in collectors:
                collector.submit("Taichung", f"R{i % 8}", route_rows(T0 + (i // 8) * 30))
        for collector in collectors:
            self.assertTrue(collector.close())
        written = sum(c.status()["snapshots_written"] for c in collectors)
        failures = sum(c.status()["write_failures"] for c in collectors)
        self.assertEqual((written, failures), (40, 0))
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(40,)])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_predictions"), [(120,)])

    def test_several_processes_can_create_and_fill_a_new_file_together(self) -> None:
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "import test_eta_observations as t\n"
            "from app.eta_observations import EtaObservationConfig, EtaObservationStore, prepare_snapshot\n"
            "store = EtaObservationStore(EtaObservationConfig(db_path=Path(sys.argv[1]), cities=frozenset({'Taichung'})))\n"
            "for i in range(30):\n"
            "    store.write([prepare_snapshot('Taichung', f'R{i}', t.route_rows(), observed_at=1, truncated=False)])\n"
            "store.close()\n"
        )
        workers = [
            subprocess.Popen([sys.executable, "-c", script, str(self.db)], cwd=REPO, stderr=subprocess.PIPE)
            for _ in range(3)
        ]
        for worker in workers:
            _, stderr = worker.communicate(timeout=120)
            self.assertEqual(worker.returncode, 0, stderr.decode(errors="replace")[-600:])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(30,)])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_predictions"), [(90,)])
        self.assertEqual(query(self.db, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(query(self.db, "PRAGMA auto_vacuum"), [(2,)])

    def test_unwritable_database_counts_failures_and_never_raises(self) -> None:
        self.db.mkdir()  # a directory where the file should be
        collector = self.collector()
        collector.start()
        self.assertTrue(collector.submit("Taichung", "TXG307", route_rows()))
        self.assertTrue(wait_for(lambda: collector.status()["write_failures"] >= 1))
        status = collector.status()
        self.assertGreaterEqual(status["snapshots_lost_to_failures"], 1)
        self.assertIn("Error", status["last_error"])
        self.assertIsNotNone(status["last_error_at"])
        self.assertTrue(collector.submit("Taichung", "TXG307", route_rows(T0 + 30)))  # still accepts, still alive
        self.assertTrue(collector.close())

    def test_corrupt_database_is_moved_aside_and_collection_resumes(self) -> None:
        self.db.write_bytes(b"this is not a sqlite database " * 200)
        collector = self.collector()
        collector.start()
        src = [T0]

        def recovered() -> bool:
            src[0] += 30
            collector.submit("Taichung", "TXG307", route_rows(src[0]))
            return collector.status()["snapshots_written"] >= 1

        self.assertTrue(wait_for(recovered, timeout=10))
        self.assertTrue(collector.close())
        status = collector.status()
        self.assertEqual(status["corruption_recoveries"], 1)
        self.assertEqual(len(list(self.dir.glob("eta_observations.db.corrupt-*"))), 1)
        self.assertEqual(query(self.db, "PRAGMA integrity_check"), [("ok",)])

    def test_disk_full_pauses_collection_and_counts_what_is_skipped(self) -> None:
        collector = self.collector()
        with patch.object(collector._store, "write", side_effect=sqlite3.OperationalError("database or disk is full")):
            collector.start()
            collector.submit("Taichung", "TXG307", route_rows())
            self.assertTrue(wait_for(lambda: collector.status()["paused_reason"] == "disk_full"))
            self.assertFalse(collector.submit("Taichung", "TXG307", route_rows(T0 + 30)))
        status = collector.status()
        self.assertEqual((status["state"], status["write_failures"], status["dropped_while_paused"]), ("paused", 1, 1))
        self.assertTrue(collector.close())

    def test_low_free_space_pauses_then_resumes(self) -> None:
        free = [10]
        collector = self.collector(min_free_bytes=1000, maintenance_interval=0.05, free_bytes=lambda _: free[0])
        collector.start()
        self.assertTrue(wait_for(lambda: collector.status()["paused_reason"] == "low_disk_space"))
        self.assertFalse(collector.submit("Taichung", "TXG307", route_rows()))
        self.assertEqual(collector.status()["free_bytes"], 10)
        free[0] = 10_000
        self.assertTrue(wait_for(lambda: collector.status()["state"] == "running"))
        self.assertTrue(collector.submit("Taichung", "TXG307", route_rows()))
        self.assertTrue(collector.close())
        self.assertEqual(collector.status()["snapshots_written"], 1)

    def test_maintenance_runs_on_the_writer_thread_and_is_reported(self) -> None:
        now = T0 + 40 * 86400
        collector = self.collector(maintenance_interval=0.05, clock=lambda: now)
        collector.start()
        collector.submit("Taichung", "OLD", route_rows(T0))
        collector.submit("Taichung", "NEW", route_rows(now - 60))
        self.assertTrue(wait_for(lambda: collector.status()["expired_snapshots"] == 1))
        self.assertTrue(collector.close())
        status = collector.status()
        self.assertEqual(status["last_maintenance_at"], now)
        self.assertGreater(status["disk_bytes"], 0)
        self.assertEqual(query(self.db, "SELECT routeid FROM eta_snapshots"), [("NEW",)])


def _sqlite_error(code: int, message: str) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


def _damage(path: Path) -> None:
    """Overwrite everything after the first pages of a real database."""
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        handle.seek(4096 * 3)
        handle.write(os.urandom(size - 4096 * 3))


class ErrorClassificationTests(_TempCase):
    def test_real_sqlite_errors_fall_into_the_right_class(self) -> None:
        holder = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("PRAGMA journal_mode = WAL")
        holder.execute("CREATE TABLE t(x)")
        holder.execute("BEGIN IMMEDIATE")
        other = sqlite3.connect(self.db, isolation_level=None, timeout=0.05)
        self.addCleanup(other.close)
        with self.assertRaises(sqlite3.Error) as locked:
            other.execute("BEGIN IMMEDIATE")
        holder.execute("COMMIT")
        other.execute("PRAGMA max_page_count = 4")
        with self.assertRaises(sqlite3.Error) as full:
            other.executemany("INSERT INTO t VALUES (?)", [("x" * 3000,) for _ in range(50)])
        garbage = self.dir / "garbage.db"
        garbage.write_bytes(b"this is not a sqlite database " * 200)
        with self.assertRaises(sqlite3.Error) as notadb:
            query(garbage, "SELECT COUNT(*) FROM sqlite_master")
        with self.assertRaises(sqlite3.Error) as cantopen:
            query(self.dir, "SELECT 1")
        self.assertEqual(
            [classify_error(c.exception) for c in (locked, full, notadb, cantopen)],
            ["busy", "full", "suspect", "other"],
        )

    def test_io_errors_and_unknown_errors_are_never_corruption(self) -> None:
        cases = {
            "other": [
                _sqlite_error(10, "disk I/O error"), _sqlite_error(522, "disk I/O error"),  # IOERR, IOERR_SHORT_READ
                _sqlite_error(8, "attempt to write a readonly database"), _sqlite_error(14, "unable to open database file"),
                sqlite3.OperationalError("disk I/O error"), PermissionError(13, "denied"), RuntimeError("anything"),
                _sqlite_error(10, "disk image is malformed"),  # the code decides, not the wording
            ],
            "busy": [_sqlite_error(5, "x"), _sqlite_error(6, "x"), _sqlite_error(261, "x"),
                     sqlite3.OperationalError("database is locked")],
            "full": [_sqlite_error(13, "x"), sqlite3.OperationalError("database or disk is full"), OSError(28, "no space")],
            "suspect": [_sqlite_error(11, "x"), _sqlite_error(26, "x"), _sqlite_error(267, "x"),
                        sqlite3.DatabaseError("database disk image is malformed")],
        }
        for expected, errors in cases.items():
            for error in errors:
                self.assertEqual(classify_error(error), expected, repr(error))


class QuarantineTests(_TempCase):
    def store(self, *, free_bytes=None, **overrides) -> EtaObservationStore:
        store = EtaObservationStore(self.config(**overrides), clock=lambda: T0, free_bytes=free_bytes)
        self.addCleanup(store.close)
        return store

    def copies(self) -> list[str]:
        return sorted(entry.name for entry in self.dir.glob("*.corrupt-*"))

    def names(self) -> list[str]:
        return sorted(entry.name for entry in self.dir.iterdir())

    def test_healthy_database_is_never_moved(self) -> None:
        store = self.store()
        store.write([self.prepared(route_rows())])
        self.assertEqual(store.quarantine(), ("healthy", None))
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(1,)])
        self.assertFalse(list(self.dir.glob("*.corrupt-*")))

    def test_damaged_database_moves_with_its_wal_and_nothing_is_deleted(self) -> None:
        store = self.store()
        store.write([self.prepared(route_rows(T0 + i * 30, stops=40), routeid=f"R{i}") for i in range(40)])
        store.close()
        writer = sqlite3.connect(self.db, isolation_level=None)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE only_in_wal(x)")
        writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        writer.close()
        wal = Path(str(self.db) + "-wal")
        wal_bytes = wal.read_bytes()
        self.assertGreater(len(wal_bytes), 0)
        self.db.write_bytes(b"this is not a sqlite database " * 200)

        outcome, copy = store.quarantine()
        self.assertEqual((outcome, copy.name), ("moved", f"eta_observations.db.corrupt-{T0}"))
        self.assertEqual(Path(str(copy) + "-wal").read_bytes(), wal_bytes)
        self.assertFalse(self.db.exists() or wal.exists())
        # A fresh database starts clean beside the copy.
        store.write([self.prepared(route_rows())])
        self.assertEqual(query(self.db, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(1,)])

    def test_damage_inside_a_real_database_is_confirmed_before_moving(self) -> None:
        store = self.store()
        store.write([self.prepared(route_rows(T0 + i * 30, stops=60), routeid=f"R{i}") for i in range(60)])
        store.close()
        _damage(self.db)
        self.assertEqual(store.quarantine()[0], "moved")

    def test_database_open_in_another_store_is_left_alone(self) -> None:
        self.db.write_bytes(b"this is not a sqlite database " * 200)
        other = self.store()
        other._hold_guard()  # what open() does before touching the database
        store = self.store()
        before = self.names()
        self.assertEqual(store.quarantine(), ("in_use", None))
        self.assertEqual(self.names(), before)
        other.close()
        self.assertEqual(store.quarantine()[0], "moved")

    def test_database_open_in_another_process_is_left_alone(self) -> None:
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from app.eta_observations import EtaObservationConfig, EtaObservationStore\n"
            "store = EtaObservationStore(EtaObservationConfig(db_path=Path(sys.argv[1]), cities=frozenset()))\n"
            "store.open()\n"
            "print('open', flush=True)\n"
            "sys.stdin.read()\n"
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", script, str(self.db)], cwd=REPO,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(worker.stdout.readline().strip(), b"open")
            store = self.store()
            with patch.object(store, "_verify", return_value="corrupt"):
                self.assertEqual(store.quarantine(), ("in_use", None))
                self.assertTrue(self.db.exists())
                worker.kill()  # a killed worker lets go of its lock
                worker.wait(timeout=30)
                self.assertEqual(store.quarantine()[0], "moved")
        finally:
            worker.kill()
            worker.communicate()

    def test_file_that_cannot_be_checked_is_left_alone(self) -> None:
        store = self.store()
        store.write([self.prepared(route_rows())])
        store.close()
        with patch.object(store, "_verify", return_value="unknown"):
            self.assertEqual(store.quarantine(), ("unknown", None))
        self.assertTrue(self.db.exists())

    def test_copy_that_would_exceed_the_byte_budget_is_not_made(self) -> None:
        garbage = b"this is not a sqlite database " * 200  # 6000 bytes
        self.db.write_bytes(garbage)
        kept = self.dir / "eta_observations.db.corrupt-1"
        kept.write_bytes(b"x" * 3000)
        Path(str(kept) + "-wal").write_bytes(b"x" * 1000)
        store = self.store(quarantine_max_bytes=9_999)  # 4000 kept + 6000 to move is one byte too many
        self.assertEqual((store.quarantined_copies(), store.quarantine_bytes()), (1, 4000))
        before = self.copies()
        self.assertEqual(store.quarantine(), ("copies_over_budget", None))
        self.assertEqual(self.copies(), before)
        self.assertEqual(self.db.read_bytes(), garbage)
        roomy = self.store(quarantine_max_bytes=10_000)
        self.assertEqual(roomy.quarantine()[0], "moved")
        self.assertEqual((roomy.quarantined_copies(), roomy.quarantine_bytes()), (2, 10_000))
        self.assertFalse(self.db.exists())

    def test_budget_counts_the_wal_that_travels_with_the_database(self) -> None:
        store = self.store()
        store.write([self.prepared(route_rows(T0 + i * 30, stops=40), routeid=f"R{i}") for i in range(40)])
        store.close()
        writer = sqlite3.connect(self.db, isolation_level=None)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE only_in_wal(x)")
        writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        writer.close()
        garbage = b"this is not a sqlite database " * 200
        self.db.write_bytes(garbage)
        wal = Path(str(self.db) + "-wal").stat().st_size
        self.assertGreater(wal, 0)
        # Room for the main file alone is not room for the copy.
        tight = self.store(quarantine_max_bytes=len(garbage) + wal - 1)
        self.assertEqual(tight.quarantine(), ("copies_over_budget", None))
        self.assertEqual((self.db.read_bytes(), self.copies()), (garbage, []))
        roomy = self.store()
        self.assertEqual(roomy.quarantine()[0], "moved")
        self.assertGreaterEqual(roomy.quarantine_bytes(), len(garbage) + wal)

    def test_budget_of_zero_never_sets_a_database_aside(self) -> None:
        garbage = b"this is not a sqlite database " * 200
        self.db.write_bytes(garbage)
        self.assertEqual(self.store(quarantine_max_bytes=0).quarantine(), ("copies_over_budget", None))
        self.assertEqual((self.db.read_bytes(), self.copies()), (garbage, []))

    def test_volume_below_its_reserve_blocks_the_move(self) -> None:
        garbage = b"this is not a sqlite database " * 200
        self.db.write_bytes(garbage)
        free = [999]
        store = self.store(min_free_bytes=1000, free_bytes=lambda _: free[0])
        self.assertEqual(store.quarantine(), ("low_disk_space", None))
        self.assertEqual((self.db.read_bytes(), self.copies()), (garbage, []))
        free[0] = 1000
        self.assertEqual(store.quarantine()[0], "moved")

    def test_unreadable_free_space_blocks_the_move(self) -> None:
        def unknown(_):
            raise OSError("cannot stat the volume")

        garbage = b"this is not a sqlite database " * 200
        self.db.write_bytes(garbage)
        store = self.store(min_free_bytes=1000, free_bytes=unknown)
        self.assertEqual(store.quarantine(), ("low_disk_space", None))
        self.assertEqual((self.db.read_bytes(), self.copies()), (garbage, []))

    def test_only_copies_of_this_database_count(self) -> None:
        store = self.store(db_path=self.dir / "eta[1].db")
        for name, size in (
            ("eta[1].db.corrupt-5", 10), ("eta[1].db.corrupt-5-wal", 5),
            ("eta1.db.corrupt-5", 100),   # what an unescaped "[1]" would match instead
            ("other.db.corrupt-5", 100), ("eta[1].db", 100),
        ):
            (self.dir / name).write_bytes(b"x" * size)
        self.assertEqual((store.quarantined_copies(), store.quarantine_bytes()), (1, 15))


class FailureHandlingTests(_TempCase):
    def run_failing(self, error, **kwargs):
        collector = self.collector(**kwargs)
        with patch.object(collector._store, "write", side_effect=error):
            collector.start()
            collector.submit("Taichung", "TXG307", route_rows())
            self.assertTrue(wait_for(lambda: collector.status()["write_failures"] == 1))
            self.assertTrue(collector.close())
        return collector.status()

    def test_lock_full_and_io_errors_never_move_the_database(self) -> None:
        store = EtaObservationStore(self.config())
        store.write([self.prepared(route_rows())])
        store.close()
        for error, paused in (
            (_sqlite_error(5, "database is locked"), None),
            (_sqlite_error(10, "disk I/O error"), None),
            (_sqlite_error(14, "unable to open database file"), None),
            (_sqlite_error(13, "database or disk is full"), "disk_full"),
            (OSError(28, "No space left on device"), "disk_full"),
        ):
            status = self.run_failing(error)
            self.assertEqual(
                (status["paused_reason"], status["corruption_suspected"], status["corruption_recoveries"]),
                (paused, 0, 0), repr(error),
            )
        self.assertFalse(list(self.dir.glob("*.corrupt-*")))
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(1,)])

    def test_a_real_lock_held_by_another_writer_costs_a_batch_not_the_file(self) -> None:
        collector = self.collector()
        collector.start()
        collector.submit("Taichung", "R0", route_rows())
        self.assertTrue(wait_for(lambda: collector.status()["snapshots_written"] == 1))
        blocker = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        collector.submit("Taichung", "R1", route_rows(T0 + 30))
        self.assertTrue(wait_for(lambda: collector.status()["write_failures"] == 1, timeout=15))
        blocker.execute("ROLLBACK")
        self.assertTrue(collector.close())
        status = collector.status()
        self.assertIn("locked", status["last_error"])
        self.assertEqual((status["corruption_suspected"], status["paused_reason"]), (0, None))
        self.assertFalse(list(self.dir.glob("*.corrupt-*")))
        self.assertEqual(query(self.db, "PRAGMA integrity_check"), [("ok",)])

    def test_a_real_full_database_pauses_and_keeps_what_was_stored(self) -> None:
        collector = self.collector()
        real_open = collector._store.open

        def tiny():
            connection = real_open()
            connection.execute(f"PRAGMA max_page_count = {connection.execute('PRAGMA page_count').fetchone()[0] + 2}")
            return connection

        with patch.object(collector._store, "open", side_effect=tiny):
            collector.start()
            collector.submit("Taichung", "SMALL", route_rows())
            self.assertTrue(wait_for(lambda: collector.status()["snapshots_written"] == 1))
            for i in range(8):
                collector.submit("Taichung", f"BIG{i}", route_rows(T0 + 30, stops=400))
            self.assertTrue(wait_for(lambda: collector.status()["paused_reason"] == "disk_full"))
            self.assertTrue(collector.close())
        self.assertEqual(collector.status()["corruption_suspected"], 0)
        self.assertFalse(list(self.dir.glob("*.corrupt-*")))
        self.assertEqual(query(self.db, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(query(self.db, "SELECT routeid FROM eta_snapshots"), [("SMALL",)])

    def test_corruption_report_about_a_healthy_file_does_not_move_it(self) -> None:
        store = EtaObservationStore(self.config())
        store.write([self.prepared(route_rows())])
        store.close()
        status = self.run_failing(_sqlite_error(11, "database disk image is malformed"))
        self.assertEqual((status["corruption_suspected"], status["corruption_recoveries"]), (1, 0))
        self.assertFalse(list(self.dir.glob("*.corrupt-*")))
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(1,)])

    def test_corrupt_database_in_use_elsewhere_is_deferred(self) -> None:
        self.db.write_bytes(b"this is not a sqlite database " * 200)
        other = EtaObservationStore(self.config())
        other._hold_guard()
        self.addCleanup(other.close)
        collector = self.collector()
        collector.start()
        collector.submit("Taichung", "TXG307", route_rows())
        self.assertTrue(wait_for(lambda: collector.status()["quarantine_deferred"] == 1, timeout=10))
        self.assertTrue(collector.close())
        self.assertEqual(collector.status()["corruption_recoveries"], 0)
        self.assertFalse(list(self.dir.glob("*.corrupt-*")))

    def test_repeated_corruption_stops_after_a_few_copies(self) -> None:
        for i in range(3):
            (self.dir / f"eta_observations.db.corrupt-{i}").write_bytes(b"x")
            (self.dir / f"eta_observations.db.corrupt-{i}-wal").write_bytes(b"x")
        self.db.write_bytes(b"this is not a sqlite database " * 200)
        collector = self.collector()
        collector.start()
        collector.submit("Taichung", "TXG307", route_rows())
        self.assertTrue(wait_for(lambda: collector.status()["paused_reason"] == "corrupt"))
        self.assertTrue(collector.close())
        self.assertEqual(len(list(self.dir.glob("*.corrupt-*"))), 6)
        self.assertTrue(self.db.exists())
        status = collector.status()
        self.assertEqual(
            (status["quarantine_blocked"], status["quarantined_copies"], status["quarantine_bytes"]),
            ("too_many_copies", 3, 6),
        )

    def test_no_room_for_another_copy_pauses_and_leaves_the_database(self) -> None:
        garbage = b"this is not a sqlite database " * 200
        self.db.write_bytes(garbage)
        collector = self.collector(quarantine_max_bytes=len(garbage) - 1)
        collector.start()
        collector.submit("Taichung", "TXG307", route_rows())
        self.assertTrue(wait_for(lambda: collector.status()["paused_reason"] == "corrupt"))
        self.assertFalse(collector.submit("Taichung", "TXG307", route_rows(T0 + 30)))  # paused: nothing queued
        self.assertTrue(collector.close())
        status = collector.status()
        self.assertEqual(
            (status["state"], status["quarantine_blocked"], status["corruption_suspected"],
             status["corruption_recoveries"], status["quarantined_copies"], status["quarantine_bytes"],
             status["dropped_while_paused"]),
            ("stopped", "copies_over_budget", 1, 0, 0, 0, 1),
        )
        self.assertEqual(self.db.read_bytes(), garbage)
        self.assertFalse(list(self.dir.glob("*.corrupt-*")))

    def test_space_gone_by_the_time_of_the_move_pauses_instead(self) -> None:
        garbage = b"this is not a sqlite database " * 200
        self.db.write_bytes(garbage)
        free = [10_000]
        collector = self.collector(min_free_bytes=1000, free_bytes=lambda _: free[0])
        collector.start()
        self.assertTrue(wait_for(lambda: collector.status()["free_bytes"] == 10_000))  # started with room
        free[0] = 10
        collector.submit("Taichung", "TXG307", route_rows())
        self.assertTrue(wait_for(lambda: collector.status()["paused_reason"] == "corrupt"))
        self.assertTrue(collector.close())
        status = collector.status()
        self.assertEqual((status["quarantine_blocked"], status["corruption_recoveries"]), ("low_disk_space", 0))
        self.assertEqual(self.db.read_bytes(), garbage)
        self.assertFalse(list(self.dir.glob("*.corrupt-*")))

    def test_second_corruption_within_the_hour_pauses_without_another_copy(self) -> None:
        self.db.write_bytes(b"this is not a sqlite database " * 200)
        collector = self.collector()
        collector.start()
        src = [T0]

        def recovered() -> bool:
            src[0] += 30
            collector.submit("Taichung", "TXG307", route_rows(src[0]))
            return collector.status()["snapshots_written"] >= 1

        self.assertTrue(wait_for(recovered, timeout=10))
        self.assertTrue(wait_for(lambda: collector.status()["queued"] == 0))
        with patch.object(
            collector._store, "write", side_effect=_sqlite_error(11, "database disk image is malformed"),
        ):
            collector.submit("Taichung", "TXG307", route_rows(src[0] + 30))
            self.assertTrue(wait_for(lambda: collector.status()["paused_reason"] == "corrupt"))
        self.assertTrue(collector.close())
        status = collector.status()
        self.assertEqual(
            (status["quarantine_blocked"], status["corruption_recoveries"], status["quarantined_copies"]),
            ("too_soon", 1, 1),
        )


class ShutdownAndCrashTests(_TempCase):
    def test_clean_shutdown_writes_everything_already_queued(self) -> None:
        collector = self.collector(queue_capacity=256)
        for i in range(200):
            collector.submit("Taichung", f"R{i}", route_rows())
        collector.start()
        self.assertTrue(collector.close())
        status = collector.status()
        self.assertEqual((status["snapshots_written"], status["dropped_on_shutdown"], status["queued"]), (200, 0, 0))
        self.assertFalse(collector.submit("Taichung", "LATE", route_rows()))  # nothing is accepted after close

    def test_shutdown_deadline_counts_what_it_had_to_discard(self) -> None:
        collector = self.collector(batch_size=1)
        real_write = collector._store.write

        def slow(batch):
            time.sleep(0.3)
            return real_write(batch)

        with patch.object(collector._store, "write", side_effect=slow):
            for i in range(20):
                collector.submit("Taichung", f"R{i}", route_rows())
            collector.start()
            collector.close(timeout=0.5)
            self.assertTrue(wait_for(lambda: collector.status()["dropped_on_shutdown"] > 0))
            self.assertTrue(wait_for(lambda: not any(t.name == "eta-observations" for t in threading.enumerate())))
        status = collector.status()
        self.assertEqual(status["snapshots_written"] + status["dropped_on_shutdown"], 20)
        self.assertEqual(query(self.db, "SELECT COUNT(*) FROM eta_snapshots"), [(status["snapshots_written"],)])

    def test_killed_writers_leave_a_consistent_database(self) -> None:
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "import test_eta_observations as t\n"
            "from app.eta_observations import EtaObservationConfig, EtaObservationStore, prepare_snapshot\n"
            "store = EtaObservationStore(EtaObservationConfig(db_path=Path(sys.argv[1]), cities=frozenset({'Taichung'})))\n"
            "store.open()\n"
            "print('ready', flush=True)\n"
            "i = 0\n"
            "while True:\n"
            "    i += 1\n"
            "    store.write([prepare_snapshot('Taichung', f'{sys.argv[2]}-{i}-{j}', t.route_rows(t.T0 + i * 30, stops=80),\n"
            "                                  observed_at=1, truncated=False) for j in range(8)])\n"
        )
        workers = [
            subprocess.Popen([sys.executable, "-c", script, str(self.db), f"W{n}"], cwd=REPO,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for n in range(2)
        ]
        try:
            for worker in workers:
                self.assertEqual(worker.stdout.readline().strip(), b"ready")
            self.assertTrue(wait_for(lambda: query(self.db, "SELECT COUNT(*) FROM eta_snapshots")[0][0] >= 80, timeout=60))
        finally:
            for worker in workers:
                worker.kill()  # SIGKILL / TerminateProcess: no handler runs, mid-transaction
            for worker in workers:
                worker.communicate()
        self.assertEqual(query(self.db, "PRAGMA integrity_check"), [("ok",)])
        stored = query(self.db, "SELECT COUNT(*) FROM eta_snapshots")[0][0]
        self.assertGreaterEqual(stored, 80)
        self.assertEqual(stored % 8, 0)  # batches are all-or-nothing
        self.assertEqual(query(
            self.db,
            "SELECT COUNT(*) FROM eta_snapshots s WHERE s.eta_rows != "
            "(SELECT COUNT(*) FROM eta_predictions p WHERE p.snapshot_id = s.snapshot_id)",
        ), [(0,)])
        self.assertEqual(query(
            self.db, "SELECT COUNT(*) FROM eta_predictions WHERE snapshot_id NOT IN (SELECT snapshot_id FROM eta_snapshots)",
        ), [(0,)])
        # ... and the next process just carries on with the same file.
        survivor = EtaObservationStore(self.config())
        self.addCleanup(survivor.close)
        self.assertEqual(survivor.write([self.prepared(route_rows(T0 - 60), routeid="AFTER")])["snapshots"], 1)
        self.assertEqual(survivor.quarantine(), ("healthy", None))


class StatusEndpointAuthTests(unittest.TestCase):
    """The real app, real tokens and the real accounts table: no patched principal."""

    def setUp(self) -> None:
        reset_rate_limit_state()
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db_path = Path(self.temp_dir.name) / "bus.db"
        init_db(db_path)
        init_app_db(db_path.parent / "app.db")
        self.settings = admin_fixture._settings(db_path)
        app = FastAPI()
        app.state.settings = self.settings
        app.state.eta_observations = None
        app.include_router(analytics_api.router)
        self.client = TestClient(app)
        self.url = "/api/v1/admin/eta-observations"

    def tearDown(self) -> None:
        reset_rate_limit_state()
        self.temp_dir.cleanup()

    def token(self, name: str, *, admin: bool) -> str:
        login = _upsert_login(
            self.settings,
            identity=OAuthIdentity(
                provider="google", provider_user_id=name, email=f"{name}@example.test",
                display_name=name, avatar_url=None,
            ),
            device_key=str(uuid4()),
            redirect_uri="https://bus.example.test/account",
        )
        if admin:
            with get_connection(self.settings.app_db_path) as connection:
                connection.execute("UPDATE accounts SET role = 'admin' WHERE id = ?", (login.account_id,))
                connection.commit()
        return login.token

    def with_cookie(self, token: str) -> int:
        self.client.cookies.set("yabus_auth_token", token)
        try:
            return self.client.get(self.url).status_code
        finally:
            self.client.cookies.clear()

    def test_only_a_real_administrator_token_gets_in(self) -> None:
        user, admin = self.token("plain-user", admin=False), self.token("the-admin", admin=True)
        bearer = lambda value: {"Authorization": f"Bearer {value}"}  # noqa: E731
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.client.get(self.url, headers=bearer("not-a-token")).status_code, 401)
        self.assertEqual(self.client.get(self.url, headers=bearer(user)).status_code, 403)
        self.assertEqual(self.with_cookie(user), 403)
        allowed = self.client.get(self.url, headers=bearer(admin))
        self.assertEqual((allowed.status_code, allowed.json()), (200, {"enabled": False}))
        self.assertEqual(self.with_cookie(admin), 200)

    def test_demoted_administrator_loses_access(self) -> None:
        admin = self.token("the-admin", admin=True)
        headers = {"Authorization": f"Bearer {admin}"}
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 200)
        with get_connection(self.settings.app_db_path) as connection:
            connection.execute("UPDATE accounts SET role = 'user'")
            connection.commit()
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 403)

    def test_live_answer_says_which_worker_it_is(self) -> None:
        self.client.app.state.eta_observations = EtaObservationCollector(
            EtaObservationConfig(db_path=Path(self.temp_dir.name) / "eta.db", cities=frozenset({"Taichung"}))
        )
        admin = self.token("the-admin", admin=True)
        body = self.client.get(self.url, headers={"Authorization": f"Bearer {admin}"}).json()
        self.assertEqual(
            (body["enabled"], body["worker_pid"], body["counters_scope"]), (True, os.getpid(), "single_worker"),
        )

    def test_route_is_registered_on_the_main_app_with_the_guard(self) -> None:
        from app.main import app as main_app

        self.assertEqual(main_app.url_path_for("get_eta_observation_status"), self.url)


class _CountingClient(realtime_fixture._FakeTDXClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.truncated = False
        self.status_code = 200

    def fetch_estimated_time_of_arrival_batch(self, city, routeids, *, if_modified_since=None):
        self.calls.append("eta")
        response = super().fetch_estimated_time_of_arrival_batch(city, routeids, if_modified_since=if_modified_since)
        if self.status_code == 304:
            return TDXJSONResponse(payload=[], status_code=304, last_modified=None)
        response.truncated = self.truncated
        return response

    def fetch_realtime_by_frequency_batch(self, city, routeids, *, if_modified_since=None):
        self.calls.append("buses")
        return super().fetch_realtime_by_frequency_batch(city, routeids, if_modified_since=if_modified_since)


class RealtimeIntegrationTests(_TempCase):
    _seed_route = realtime_fixture.RealtimeBackfillTests._seed_route

    def setUp(self) -> None:
        super().setUp()
        outer_dir, outer_db = self.temp_dir, self.db
        realtime_fixture.RealtimeBackfillTests.setUp(self)  # static db with route TXG307
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_dir, self.db = outer_dir, outer_db
        self.client = _CountingClient()
        now = int(time.time())
        self.client.eta_payload_by_route["TXG307"] = [
            row(1, eta=120, src=now, extra={"StopID": "STOP1"}),
            row(2, eta=240, src=now, extra={"StopID": "STOP2"}),
            row(3, status=1, src=now, next_bus=now + 900, extra={"StopID": "STOP3"}),
        ]

    def service(self, collector=None) -> RealtimeService:
        return RealtimeService(
            self.settings, self.client,
            route_buses_service=RouteBusesService(self.settings, self.client),
            ntpc_opendata_client=self.ntpc_client, eta_observations=collector,
        )

    @staticmethod
    def shape(snapshot):
        return [
            (path["pathid"], stop["stopid"], stop["message"], stop["eta"] is not None,
             [(eta.get("plate"), eta.get("source")) for eta in stop["etas"]])
            for path in snapshot["paths"] for stop in path["stops"]
        ]

    def test_collecting_adds_no_upstream_call_and_changes_no_response(self) -> None:
        plain = self.service().get_snapshot("TXG307")
        calls_without = list(self.client.calls)
        self.client.calls.clear()

        collector = self.collector()
        collector.start()
        observed = self.service(collector).get_snapshot("TXG307")
        self.assertEqual(self.client.calls, calls_without)
        self.assertEqual(self.shape(observed), self.shape(plain))
        self.assertTrue(collector.close())
        self.assertEqual(
            query(self.db, "SELECT city, routeid, stop_rows, eta_rows, truncated FROM eta_snapshots"),
            [("Taichung", "TXG307", 3, 2, 0)],
        )
        self.assertEqual(
            query(self.db, "SELECT stop_seq, plate, eta_seconds, stop_status FROM eta_predictions ORDER BY 1"),
            [(1, "AAA-001", 120, 0), (2, "AAA-001", 240, 0)],
        )
        self.assertEqual(query(self.db, "SELECT stop_seq, stop_status FROM eta_stop_states"), [(3, 1)])

    def test_a_broken_collector_cannot_break_a_response(self) -> None:
        class Exploding:
            def wants(self, city):
                raise RuntimeError("boom")

        snapshot = self.service(Exploding()).get_snapshot("TXG307")
        self.assertEqual(snapshot["routeid"], "TXG307")
        self.assertEqual(len(self.shape(snapshot)), 3)

    def test_a_dead_disk_does_not_slow_or_fail_requests(self) -> None:
        self.db.mkdir()
        collector = self.collector()
        collector.start()
        service = self.service(collector)
        started = time.perf_counter()
        snapshot = service.get_snapshot("TXG307")
        elapsed = time.perf_counter() - started
        self.assertEqual(len(self.shape(snapshot)), 3)
        self.assertLess(elapsed, 2.0)
        self.assertTrue(wait_for(lambda: collector.status()["write_failures"] >= 1))

    def test_truncated_batches_are_marked(self) -> None:
        self.client.truncated = True
        collector = self.collector()
        collector.start()
        self.service(collector).get_snapshot("TXG307")
        collector.close()
        self.assertEqual(query(self.db, "SELECT truncated FROM eta_snapshots"), [(1,)])

    def test_not_modified_refresh_stores_nothing_new(self) -> None:
        collector = self.collector()
        collector.start()
        service = self.service(collector)
        service.get_snapshot("TXG307")
        self.client.status_code = 304
        with service._cache_lock:
            for entry in service._cache.values():
                entry.expires_at = 0.0
        service.get_snapshot("TXG307")
        collector.close()
        self.assertEqual(self.client.calls.count("eta"), 2)
        self.assertEqual(collector.status()["submitted"], 1)

    def test_other_cities_and_synthetic_fallback_rows_are_not_stored(self) -> None:
        self._seed_route("NWT100")
        now = int(time.time())
        self.client.buses_payload_by_route["NWT100"] = [{
            "PlateNumb": "NTP-001", "SubRouteUID": "NWT100", "RouteUID": "NWT100", "Direction": 0,
            "BusPosition": {"PositionLat": 24.1, "PositionLon": 120.65}, "GPSTime": iso(now),
        }]
        collector = self.collector(cities=frozenset({"Taichung", "NewTaipei"}))
        collector.start()
        self.service(collector).get_snapshot("NWT100")  # TDX has no ETA: NTPC fallback path
        collector.close()
        self.assertEqual(collector.status()["submitted"], 0)
        self.assertFalse(self.db.exists() and query(self.db, "SELECT COUNT(*) FROM eta_snapshots")[0][0])


class TruncationFlagTests(unittest.TestCase):
    def client(self, pages):
        client = TDXClient.__new__(TDXClient)
        client.settings = None
        responses = iter(pages)
        client._request_json_with_meta = lambda *args, **kwargs: TDXJSONResponse(
            payload=next(responses), status_code=200, last_modified=None
        )
        client._normalize_items = lambda payload: payload
        return client

    def test_single_chunk_at_the_cap_is_flagged(self) -> None:
        full = self.client([[{}] * 2000])._fetch_subroute_batch("/x", ["A"])
        short = self.client([[{}] * 1999])._fetch_subroute_batch("/x", ["A"])
        self.assertEqual((full.truncated, short.truncated, len(full.payload)), (True, False, 2000))

    def test_any_chunk_at_the_cap_flags_the_merged_response(self) -> None:
        routeids = [f"R{i}" for i in range(30)]  # two chunks of the filter
        response = self.client([[{}] * 10, [{}] * 2000])._fetch_subroute_batch("/x", routeids)
        self.assertTrue(response.truncated)
        self.assertEqual(len(response.payload), 2010)


class StatusEndpointTests(unittest.TestCase):
    def request(self, collector):
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(eta_observations=collector)))

    def test_requires_an_administrator(self) -> None:
        for principal, code in ((None, 401), (SimpleNamespace(role="user"), 403)):
            with patch.object(analytics_api, "get_request_principal", return_value=principal):
                with self.assertRaises(HTTPException) as caught:
                    analytics_api.get_eta_observation_status(self.request(None))
            self.assertEqual(caught.exception.status_code, code)

    def test_reports_disabled_or_live_counters(self) -> None:
        with patch.object(analytics_api, "get_request_principal", return_value=SimpleNamespace(role="admin")):
            self.assertEqual(analytics_api.get_eta_observation_status(self.request(None)), {"enabled": False})
            with tempfile.TemporaryDirectory() as folder:
                collector = EtaObservationCollector(
                    EtaObservationConfig(db_path=Path(folder) / "e.db", cities=frozenset({"Taichung"}))
                )
                status = analytics_api.get_eta_observation_status(self.request(collector))
        for key in ("enabled", "state", "queued", "dropped_queue_full", "write_failures", "snapshots_written",
                    "last_error", "last_write_at", "disk_bytes", "free_bytes", "paused_reason",
                    "worker_pid", "counters_scope", "quarantine_max_bytes", "quarantined_copies",
                    "quarantine_bytes", "quarantine_blocked"):
            self.assertIn(key, status)
        self.assertEqual((status["enabled"], status["state"]), (True, "stopped"))
        self.assertEqual(
            (status["worker_pid"], status["quarantined_copies"], status["quarantine_bytes"]), (os.getpid(), 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
