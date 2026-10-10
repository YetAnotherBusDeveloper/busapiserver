from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import date
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app.journey_gtfs import atomic_publish, digest, import_gtfs, json_bytes, main
from app.journeys import JourneyQuery, Router, load_router
from test_journeys import oracle, query, stop_time, timetable, trip


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "app" / "data" / "gtfs-synthetic-v1"
WALKING = ROOT / "app" / "data" / "gtfs-synthetic-walking-v1.json"
DAY = date(2026, 10, 10)


class GTFSImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="journey-gtfs-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.feed = self.root / "feed"
        self.feed.mkdir()
        self.write("agency.txt", [dict(agency_id="agency", agency_name="Fictional Agency",
                                     agency_url="https://example.invalid", agency_timezone="Asia/Taipei")])
        self.stops = [dict(stop_id=s, stop_name=f"Fictional {s}", stop_lat="25", stop_lon="121", location_type="0")
                      for s in "ABCD"]
        self.write("stops.txt", self.stops)
        self.routes = [dict(route_id="R", route_short_name="Fictional", route_type="3", agency_id="agency")]
        self.write("routes.txt", self.routes)
        self.trips = [dict(trip_id="t", route_id="R", service_id="WEEK")]
        self.write("trips.txt", self.trips)
        self.times = [self.event("A", 1, "08:00:00"), self.event("B", 2, "08:10:00"), self.event("D", 3, "08:20:00")]
        self.write("stop_times.txt", self.times)
        self.calendar = dict(service_id="WEEK", monday="1", tuesday="1", wednesday="1", thursday="1",
                             friday="1", saturday="1", sunday="1", start_date="20261008", end_date="20261012")
        self.write("calendar.txt", [self.calendar])

    def write(self, name, rows, fields=None):
        fields = fields or list(dict.fromkeys(key for row in rows for key in row))
        with (self.feed / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def event(self, stop, sequence, arrival, departure=None, **values):
        return dict(trip_id="t", stop_id=stop, stop_sequence=str(sequence), arrival_time=arrival,
                    departure_time=arrival if departure is None else departure) | values

    def convert(self, path=None, **options):
        return import_gtfs(path or self.feed, **(dict(service_date=DAY, feed_id="test", source="Fictional test data",
                                                     synthetic=True) | options))

    def assert_rejected(self, code):
        table, report = self.convert()
        self.assertIsNone(table)
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["fatal_errors"][0]["code"], code)

    def assert_excluded(self, code):
        table, report = self.convert()
        self.assertIsNotNone(table)
        self.assertEqual(table.trips, ())
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["excluded_trips"][0]["trip_id"], "t")
        self.assertIn(code, [reason["code"] for reason in report["excluded_trips"][0]["reasons"]])

    def cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [str(self.feed), "--date", DAY.isoformat(), "--feed-id", "test", "--source", "Fictional test data",
                "--synthetic", "--output", str(self.root / "timetable.json"), "--report", str(self.root / "report.json")]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(args + list(extra))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_calendar_weekdays_inclusive_bounds_and_exceptions(self):
        self.calendar.update(start_date="20261010", end_date="20261011", saturday="0")
        self.write("calendar.txt", [self.calendar])
        table, report = self.convert()
        self.assertEqual([run["service_date"] for run in report["imported_runs"]], ["2026-10-11"])
        self.assertEqual(table.trips[0].stop_times[0].departure, 115200)
        self.write("calendar_dates.txt", [dict(service_id="WEEK", date="20261010", exception_type="1"),
                                          dict(service_id="WEEK", date="20261011", exception_type="2")])
        table, report = self.convert()
        self.assertEqual([run["service_date"] for run in report["imported_runs"]], ["2026-10-10"])
        self.assertEqual(table.trips[0].stop_times[0].departure, 28800)
        self.calendar.update(start_date="20261001", end_date="20261009")
        self.write("calendar.txt", [self.calendar])
        # An explicit addition applies even outside the weekly date interval.
        self.assertEqual(len(self.convert()[0].trips), 1)

    def test_calendar_dates_only_additions_and_holiday_removal(self):
        (self.feed / "calendar.txt").unlink()
        self.write("calendar_dates.txt", [dict(service_id="WEEK", date="20261010", exception_type="1")])
        table, report = self.convert()
        self.assertEqual(len(table.trips), 1)
        self.assertEqual(report["imported_runs"][0]["service_date"], "2026-10-10")
        self.write("calendar_dates.txt", [dict(service_id="WEEK", date="20261010", exception_type="2")])
        table, report = self.convert()
        self.assertEqual(table.trips, ())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["not_selected"][0]["reason"], "inactive_in_overlapping_service_dates")

    def test_duplicate_or_invalid_calendars_are_rejected(self):
        for rows, code in [
            ([dict(service_id="WEEK", date="20261010", exception_type="1")] * 2, "duplicate_exception"),
            ([dict(service_id="WEEK", date="20260230", exception_type="1")], "invalid_date"),
            ([dict(service_id="WEEK", date="2026-10-10", exception_type="1")], "invalid_date"),
            ([dict(service_id="WEEK", date="20261010", exception_type="3")], "invalid_enum"),
        ]:
            with self.subTest(rows=rows):
                self.write("calendar_dates.txt", rows)
                self.assert_rejected(code)
        (self.feed / "calendar_dates.txt").unlink()
        self.write("calendar.txt", [self.calendar, self.calendar])
        self.assert_rejected("duplicate_id")
        self.write("calendar.txt", [self.calendar | {"end_date": "20261001"}])
        self.assert_rejected("invalid_calendar_range")
        self.write("calendar.txt", [self.calendar | {"saturday": "2"}])
        self.assert_rejected("invalid_enum")

    def test_prior_service_spillover_current_and_next_day(self):
        self.write("stop_times.txt", [self.event("A", 1, "23:50:00"), self.event("B", 2, "24:05:00"),
                                      self.event("D", 3, "24:20:00")])
        table, report = self.convert()
        self.assertEqual([(r["service_date"], r["stop_times"]) for r in report["imported_runs"]],
                         [("2026-10-09", 2), ("2026-10-10", 3)])
        self.assertEqual([s.arrival for s in table.trips[0].stop_times], [300, 1200])
        self.assertEqual([s.arrival for s in table.trips[1].stop_times], [85800, 86700, 87600])
        self.assertEqual(report["omitted_runs"][0]["service_date"], "2026-10-11")
        self.assertEqual(report["summary"]["clipped_runs"], 1)
        self.write("calendar_dates.txt", [dict(service_id="WEEK", date="20261009", exception_type="2")])
        table, report = self.convert()
        self.assertEqual([r["service_date"] for r in report["imported_runs"]], ["2026-10-10"])

    def test_spillover_more_than_one_service_day_and_stable_run_ids(self):
        self.write("stop_times.txt", [self.event("A", 1, "49:00:00"), self.event("D", 2, "49:30:00")])
        self.write("calendar.txt", [self.calendar | {"end_date": "20261008"}])
        table, report = self.convert()
        self.assertEqual(report["imported_runs"][0]["service_date"], "2026-10-08")
        self.assertEqual([s.arrival for s in table.trips[0].stop_times], [3600, 5400])
        earlier, _ = self.convert(service_date=date(2026, 10, 9))
        self.assertEqual(table.trips[0].id, earlier.trips[0].id)
        self.assertEqual([s.arrival for s in earlier.trips[0].stop_times], [90000, 91800])

    def test_boundary_dwell_is_reported_not_fabricated_or_clamped(self):
        self.write("stop_times.txt", [self.event("A", 1, "23:59:00", "24:01:00"),
                                      self.event("B", 2, "24:02:00"), self.event("D", 3, "24:03:00")])
        table, report = self.convert()
        previous = report["imported_runs"][0]
        self.assertTrue(previous["discarded_events"][0]["straddles_boundary"])
        self.assertEqual([s.arrival for s in table.trips[0].stop_times], [120, 180])
        self.write("stop_times.txt", [self.event("A", 1, "23:58:00"), self.event("B", 2, "23:59:00"),
                                      self.event("D", 3, "23:59:59", "24:00:00")])
        table, report = self.convert()
        following = report["imported_runs"][-1]
        self.assertEqual(following["service_date"], "2026-10-11")
        self.assertTrue(following["discarded_events"][0]["straddles_boundary"])
        self.assertEqual([s.arrival for s in table.trips[-1].stop_times], [172680, 172740])

    def test_pickup_dropoff_defaults_and_out_of_order_stop_sequences(self):
        self.write("stop_times.txt", [self.times[2] | {"pickup_type": "1"},
                                      self.times[0] | {"drop_off_type": "1"}, self.times[1]])
        table, _ = self.convert()
        events = table.trips[0].stop_times
        self.assertEqual([s.arrival for s in events], [28800, 29400, 30000])
        self.assertTrue(events[0].pickup)
        self.assertFalse(events[0].dropoff)
        self.assertTrue(events[1].pickup and events[1].dropoff)
        self.assertFalse(events[-1].pickup)
        self.assertTrue(events[-1].dropoff)

    def test_missing_approximate_conditional_and_flex_times_are_excluded(self):
        for change, code in [
            ({"arrival_time": ""}, "incomplete_stop_times"),
            ({"departure_time": ""}, "incomplete_stop_times"),
            ({"timepoint": "0"}, "inexact_timepoint"),
            ({"pickup_type": "2"}, "conditional_boarding"),
            ({"drop_off_type": "3"}, "conditional_boarding"),
            ({"continuous_pickup": "0"}, "continuous_boarding"),
            ({"pickup_booking_rule_id": "book"}, "flex_service"),
            ({"location_group_id": "zone"}, "flex_service"),
            ({"start_pickup_drop_off_window": "08:00:00"}, "flex_service"),
        ]:
            with self.subTest(change=change):
                self.write("stop_times.txt", [self.times[0], self.times[1] | change, self.times[2]])
                self.assert_excluded(code)

    def test_frequency_templates_are_never_imported_as_single_exact_trips(self):
        for exact in ("", "0", "1"):
            with self.subTest(exact_times=exact):
                self.write("frequencies.txt", [dict(trip_id="t", start_time="08:00:00", end_time="10:00:00",
                                                   headway_secs="600", exact_times=exact)])
                self.assert_excluded("frequency_service")

    def test_invalid_times_sequences_and_references(self):
        for change, code in [
            ({"arrival_time": "07:59:59"}, "nonmonotonic_times"),
            ({"departure_time": "08:09:59"}, "nonmonotonic_times"),
            ({"arrival_time": "08:60:00"}, "invalid_time"),
            ({"arrival_time": "-1:00:00"}, "invalid_time"),
            ({"arrival_time": "8:10"}, "invalid_time"),
            ({"stop_sequence": "1"}, "duplicate_stop_sequence"),
            ({"stop_sequence": "-1"}, "invalid_integer"),
            ({"stop_id": "missing"}, "invalid_trip_stop"),
        ]:
            with self.subTest(change=change):
                self.write("stop_times.txt", [self.times[0], self.times[1] | change, self.times[2]])
                self.assert_excluded(code)
        self.write("stop_times.txt", self.times)
        self.write("trips.txt", [self.trips[0] | {"service_id": "unknown"}])
        self.assert_rejected("unknown_trip_reference")

    def test_bus_route_types_and_restricted_services(self):
        for route_type in (3, 11, 200, 209, 700, 702, 713, 714, 716, 800):
            with self.subTest(route_type=route_type):
                self.write("routes.txt", [self.routes[0] | {"route_type": str(route_type)}])
                self.assertEqual(len(self.convert()[0].trips), 2)
        for route_type in (0, 1, 2, 4, 100, 900, 1500):
            with self.subTest(route_type=route_type):
                self.write("routes.txt", [self.routes[0] | {"route_type": str(route_type)}])
                table, report = self.convert()
                self.assertEqual(table.trips, ())
                self.assertEqual(report["not_selected"][0]["reason"], "non_bus_route")
        for route_type in (707, 708, 709, 712, 715):
            with self.subTest(route_type=route_type):
                self.write("routes.txt", [self.routes[0] | {"route_type": str(route_type)}])
                self.assert_excluded("restricted_bus_type")

    def test_transfer_rules_exclude_affected_trips_but_keep_unaffected_service(self):
        self.write("stops.txt", self.stops + [dict(stop_id="S", stop_name="Fictional Station", stop_lat="25", stop_lon="121", location_type="1")])
        self.stops[1]["parent_station"] = "S"
        self.write("stops.txt", self.stops + [dict(stop_id="S", stop_name="Fictional Station", stop_lat="25", stop_lon="121", location_type="1")])
        self.write("trips.txt", self.trips + [dict(trip_id="other", route_id="R", service_id="WEEK")])
        self.write("stop_times.txt", self.times + [self.event("C", 1, "09:00:00", trip_id="other"),
                                                    self.event("D", 2, "09:20:00", trip_id="other")])
        for kind in ("0", "1", "2", "3"):
            with self.subTest(transfer_type=kind):
                self.write("transfers.txt", [dict(from_stop_id="S", to_stop_id="B", transfer_type=kind, min_transfer_time="180")])
                table, report = self.convert()
                self.assertEqual({run["trip_id"] for run in report["imported_runs"]}, {"other"})
                self.assertEqual(report["transfer_rules"][0]["affected_trip_ids"], ["t"])
                self.assertEqual(len(table.trips), 2)
        # Route-scoped rules intentionally exclude the whole named route.
        self.write("transfers.txt", [dict(from_stop_id="A", to_stop_id="B", from_route_id="R", transfer_type="3")])
        self.assertEqual(self.convert()[0].trips, ())
        for kind in ("4", "5"):
            with self.subTest(transfer_type=kind):
                self.write("transfers.txt", [dict(from_trip_id="t", to_trip_id="other", transfer_type=kind)])
                self.assertEqual(self.convert()[0].trips, ())

    def test_incomplete_or_unresolvable_transfer_rules_reject_feed(self):
        for rule, code in [
            (dict(from_stop_id="A", transfer_type="3"), "incomplete_transfer_rule"),
            (dict(from_stop_id="A", to_stop_id="unknown", transfer_type="3"), "unknown_transfer_reference"),
            (dict(from_trip_id="t", transfer_type="4"), "incomplete_transfer_rule"),
            (dict(from_stop_id="A", to_stop_id="B", transfer_type="2"), "missing_value"),
            (dict(from_stop_id="A", to_stop_id="B", transfer_type="9"), "invalid_enum"),
        ]:
            with self.subTest(rule=rule):
                self.write("transfers.txt", [rule])
                self.assert_rejected(code)

    def test_walking_config_is_explicit_directed_and_validated(self):
        config = self.root / "walking.json"
        config.write_text(json.dumps(dict(default_min_transfer_seconds=30, min_transfer_seconds={"B": 90},
                                         footpaths=[dict(from_stop="A", to_stop="C", seconds=15)])), encoding="utf-8")
        table, report = self.convert(walking_config=config)
        self.assertEqual(len(table.footpaths), 1)
        self.assertEqual(table.footpaths[0].from_stop, report["ids"]["stops"]["A"])
        self.assertEqual(table.footpaths[0].to_stop, report["ids"]["stops"]["C"])
        self.assertEqual({s.min_transfer_seconds for s in table.stops}, {30, 90})
        self.assertEqual(self.convert()[0].footpaths, ())
        for body in (dict(footpaths=[dict(from_stop="A", to_stop="C", seconds=0)]),
                     dict(min_transfer_seconds={"missing": 10}), dict(surprise=True)):
            with self.subTest(body=body):
                config.write_text(json.dumps(body), encoding="utf-8")
                self.assertIsNone(self.convert(walking_config=config)[0])

    def test_timezone_and_historical_date_constraints(self):
        table, report = self.convert(service_date=date(1979, 12, 31))
        self.assertIsNone(table)
        self.assertEqual(report["fatal_errors"][0]["code"], "unsupported_snapshot_date")
        self.write("agency.txt", [dict(agency_name="Fictional", agency_url="https://example.invalid", agency_timezone="UTC")])
        self.assert_rejected("unsupported_timezone")

    def test_agency_timezone_controls_times_not_stop_timezone(self):
        self.write("stops.txt", [row | {"stop_timezone": "Asia/Tokyo"} for row in self.stops])
        table, _ = self.convert()
        self.assertEqual(table.timezone, "Asia/Taipei")
        self.assertEqual(table.trips[0].stop_times[0].departure, 28800)

    def test_invalid_stop_hierarchy_is_rejected(self):
        for change in ({"parent_station": "unknown"}, {"parent_station": "B"},
                       {"location_type": "1", "parent_station": "A"}, {"location_type": "4"}):
            with self.subTest(change=change):
                self.write("stops.txt", [self.stops[0] | change, *self.stops[1:]])
                self.assert_rejected("invalid_parent_station")

    def test_long_unicode_ids_fit_snapshot_schema_without_losing_mapping(self):
        identifier = "虛構站牌/" * 40
        self.write("stops.txt", [self.stops[0] | {"stop_id": identifier}, *self.stops[1:]])
        self.write("stop_times.txt", [self.times[0] | {"stop_id": identifier}, *self.times[1:]])
        table, report = self.convert(feed_id="f" * 32)
        self.assertIsNotNone(table, report["fatal_errors"])
        self.assertIn(identifier, report["ids"]["stops"])
        self.assertTrue(all(len(stop.id) <= 128 for stop in table.stops))
        self.assertTrue(all(len(service.id) <= 128 for service in table.trips))

    def test_zero_duration_events_are_valid_but_overnight_wrap_is_not(self):
        self.write("stop_times.txt", [self.event("A", 1, "0:00:00"), self.event("D", 2, "0:00:00")])
        table, _ = self.convert()
        self.assertEqual([event.arrival for event in table.trips[0].stop_times], [0, 0])
        self.write("stop_times.txt", [self.event("A", 1, "23:50:00"), self.event("D", 2, "00:20:00")])
        self.assert_excluded("nonmonotonic_times")

    def test_structural_errors_and_bom(self):
        original = (self.feed / "stops.txt").read_bytes()
        for content, code in [(b"\xff", "invalid_csv"), (b"stop_id,stop_id\nA,A\n", "invalid_header"),
                              (b"other\nA\n", "missing_columns"), (b"stop_id\nA,extra\n", "invalid_csv_row")]:
            with self.subTest(content=content):
                (self.feed / "stops.txt").write_bytes(content)
                self.assert_rejected(code)
        (self.feed / "stops.txt").write_bytes(b"\xef\xbb\xbf" + original)
        self.assertIsNotNone(self.convert()[0])
        self.write("stops.txt", self.stops + [self.stops[0]])
        self.assert_rejected("duplicate_id")
        (self.feed / "stops.txt").unlink()
        self.assert_rejected("missing_tables")

    def test_directory_zip_consistency_provenance_and_stable_ids(self):
        first, report = self.convert()
        zipped = self.root / "feed.zip"
        with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.feed.iterdir(), reverse=True):
                archive.write(path, path.name)
        second, zipped_report = self.convert(zipped)
        self.assertEqual(first, second)
        self.assertEqual(report["input"]["routing_inputs_sha256"], zipped_report["input"]["routing_inputs_sha256"])
        self.assertEqual(report["timetable_sha256"], digest(json_bytes(first.model_dump(mode="json"))))
        self.write("stop_times.txt", list(reversed(self.times)))
        reordered, reordered_report = self.convert()
        self.assertEqual(first.trips, reordered.trips)
        self.assertEqual(report["ids"], reordered_report["ids"])
        self.assertNotEqual(first.dataset_id, reordered.dataset_id)  # input bytes changed
        # A namespace change prevents accidental collisions across feeds.
        different, _ = self.convert(feed_id="another-feed")
        self.assertNotEqual(first.stops[0].id, different.stops[0].id)

    def test_zip_traversal_nested_duplicate_and_size_limits(self):
        for names, code in [( ["../stops.txt"], "unsafe_zip_member"),
                            (["wrapper/stops.txt"], "nested_gtfs_table"),
                            (["stops.txt", "stops.txt"], "duplicate_zip_member")]:
            with self.subTest(names=names):
                zipped = self.root / "bad.zip"
                with zipfile.ZipFile(zipped, "w") as archive:
                    for name in names:
                        # zipfile warns about duplicates; the importer must reject them.
                        import warnings
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", UserWarning)
                            archive.writestr(name, "stop_id\nA\n")
                table, report = self.convert(zipped)
                self.assertIsNone(table)
                self.assertEqual(report["fatal_errors"][0]["code"], code)
        self.assertFalse((self.root / "stops.txt").exists())
        with patch("app.journey_gtfs.MAX_TABLE_BYTES", 1):
            self.assert_rejected("input_limit")

    def test_only_named_gtfs_tables_are_read(self):
        # Guard the reader itself: arbitrary files, including a dummy .env,
        # must not be opened. No local/environment configuration is involved.
        dummy = self.feed / ".env"
        dummy.write_text("SYNTHETIC_PLACEHOLDER=not-a-secret\n", encoding="utf-8")
        original = Path.open

        def guarded(path, *args, **kwargs):
            if path.name == ".env":
                raise AssertionError("dotenv files must not be read")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            self.assertIsNotNone(self.convert()[0])

    def test_cli_help_real_launch_and_validated_output(self):
        launched = subprocess.run([sys.executable, "-m", "app.journey_gtfs", "--help"], cwd=ROOT,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertIn("--walking-config", launched.stdout)
        status, stdout, stderr = self.cli()
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "ok")
        engine = load_router(self.root / "timetable.json")
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        request = JourneyQuery(origin=report["ids"]["stops"]["A"], destination=report["ids"]["stops"]["D"],
                               service_date=DAY, departure_seconds=28800, max_transfers=0)
        self.assertEqual(engine.route(request).journeys[0].arrival.strftime("%H:%M:%S"), "08:20:00")
        self.assertEqual(report["timetable_sha256"], digest((self.root / "timetable.json").read_bytes()))

    def test_cli_rejection_writes_report_but_no_timetable(self):
        (self.feed / "calendar.txt").unlink()
        status, stdout, _ = self.cli()
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stdout)["status"], "rejected")
        self.assertFalse((self.root / "timetable.json").exists())
        report = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["fatal_errors"][0]["code"], "missing_calendar")

    def test_cli_never_overwrites_input_output_config_or_aliases(self):
        original = (self.feed / "stops.txt").read_bytes()
        alias = self.root / "hardlink.json"
        os.link(self.feed / "stops.txt", alias)
        for extra in (("--output", str(self.feed / "stops.txt")),
                      ("--report", str(self.feed / "stops.txt")),
                      ("--output", str(self.feed / "new-output.json")),
                      ("--output", str(alias)),
                      ("--output", str(self.root / "report.json")),
                      ("--walking-config", str(alias), "--output", str(alias))):
            with self.subTest(extra=extra):
                self.assertEqual(self.cli(*extra)[0], 2)
                self.assertEqual((self.feed / "stops.txt").read_bytes(), original)
                self.assertFalse((self.root / "report.json").exists())
        self.assertEqual(self.cli()[0], 0)
        output = (self.root / "timetable.json").read_bytes()
        self.assertEqual(self.cli()[0], 2)
        self.assertEqual((self.root / "timetable.json").read_bytes(), output)

    def test_failed_snapshot_publication_leaves_report_and_preserves_other_writer(self):
        original_link = os.link

        def link(source, target):
            if Path(target).name == "timetable.json":
                Path(target).write_bytes(b"other writer")
            return original_link(source, target)

        with patch("app.journey_gtfs.os.link", side_effect=link):
            status, _, error = self.cli()
        self.assertEqual(status, 2)
        self.assertIn("GTFS import failed", error)
        self.assertTrue((self.root / "report.json").exists())
        self.assertEqual((self.root / "timetable.json").read_bytes(), b"other writer")
        self.assertEqual(list(self.root.glob(".journey-gtfs-*.tmp")), [])

    def test_atomic_publish_does_not_clobber_a_racing_writer(self):
        output = self.root / "atomic.json"
        original_link = os.link

        def race(source, target):
            Path(target).write_bytes(b"other writer")
            return original_link(source, target)

        with patch("app.journey_gtfs.os.link", side_effect=race), self.assertRaises(FileExistsError):
            atomic_publish(output, b"new timetable")
        self.assertEqual(output.read_bytes(), b"other writer")
        self.assertEqual(list(self.root.glob(".journey-gtfs-*.tmp")), [])

    def test_synthetic_fixture_routing_matches_independent_hand_authored_oracle(self):
        imported, report = self.convert(FIXTURE, walking_config=WALKING)
        self.assertIsNotNone(imported, report["fatal_errors"])
        self.assertTrue(imported.is_demo)
        self.assertEqual(report["summary"]["imported_runs"], 8)
        self.assertEqual(report["summary"]["excluded_trip_templates"], 4)
        self.assertEqual(report["summary"]["omitted_runs"], 1)
        # This reference is written directly from the fictional published
        # schedule, not from importer output or its calendar/time/ID helpers.
        services = [trip("previous-night", stop_time("B", 300), stop_time("D", 1200)),
                    trip("night", stop_time("A", 85800), stop_time("B", 86700), stop_time("D", 87600)),
                    trip("special", stop_time("A", 32400), stop_time("D", 34200)),
                    trip("next-holiday", stop_time("A", 111600), stop_time("D", 112800))]
        for shift in (0, 86400):
            services.extend([
                trip(f"local-{shift}", stop_time("A", 28800 + shift, dropoff=False),
                     stop_time("B", 29400 + shift, 29460 + shift), stop_time("D", 31500 + shift, pickup=False)),
                trip(f"express-{shift}", stop_time("C", 29640 + shift, dropoff=False),
                     stop_time("D", 30300 + shift, pickup=False))])
        reference = timetable(*services, stops="ABCDX", walks=[("B", "C", 180), ("C", "B", 180)],
                              margins={"A": 120, "B": 60, "C": 60, "D": 120, "X": 120})
        local = Router(imported)
        for origin, destination in (("A", "D"), ("B", "D"), ("B", "C"), ("A", "A"), ("A", "X")):
            for start in (0, 28800, 28801, 85800, 86400, 115200, 172799):
                for limit in (0, 1, 4):
                    with self.subTest(origin=origin, destination=destination, start=start, limit=limit):
                        request = query(origin=origin, destination=destination, departure_seconds=start, max_transfers=limit)
                        actual = local.route(request.model_copy(update={"origin": report["ids"]["stops"][origin],
                                                                         "destination": report["ids"]["stops"][destination]}))
                        objectives = [(int((journey.arrival - local.midnight).total_seconds()), journey.boardings)
                                      for journey in actual.journeys]
                        self.assertEqual(objectives, oracle(reference, request))


if __name__ == "__main__":
    unittest.main()
