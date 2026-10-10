from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.journeys import _load_configured_router, get_journey_router, router
from app.journey_demo import engine
from app.journeys import JourneyQuery, Router, Timetable


def query(**overrides):
    values = dict(origin="A", destination="D", service_date="2026-10-10",
                  departure_seconds=28800, max_transfers=2)
    return JourneyQuery(**(values | overrides))


def oracle(table, request):
    """Explore exact off-vehicle states and individual onboard arrival events.

    No Router indexes, boarding rounds, downstream-ride shortcuts, or earliest
    stop labels are used. Only identical states are deduplicated. Onboard edges
    advance ONE stop, independently of pickup/dropoff and off-vehicle dominance.
    Walking segments are simple paths: positive cycles cannot improve a journey
    when waiting is unrestricted. Boardings are bounded, even for zero-time buses.
    """
    margins = {s.id: s.min_transfer_seconds for s in table.stops}
    trips = {trip.id: trip for trip in table.trips}
    pending = [("off", request.origin, request.departure_seconds, 0, frozenset({request.origin}))]
    seen = set()
    arrivals = set()
    while pending:
        state = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        mode, place, value, boards, walked = state
        if mode == "on":
            trip = trips[place]
            event = trip.stop_times[value]
            if event.dropoff:
                pending.append(("off", event.stop_id, event.arrival, boards, frozenset({event.stop_id})))
            if value + 1 < len(trip.stop_times):
                pending.append(("on", place, value + 1, boards, frozenset()))
            continue

        now = value
        if now >= 2 * 24 * 60 * 60:
            continue
        if place == request.destination:
            arrivals.add((now, boards))
        for foot in table.footpaths:
            if foot.from_stop == place and foot.to_stop not in walked:
                pending.append(("off", foot.to_stop, now + foot.seconds, boards, walked | {foot.to_stop}))
        if boards >= request.max_transfers + 1:
            continue
        for trip in table.trips:
            for i, start in enumerate(trip.stop_times[:-1]):
                if start.stop_id != place or not start.pickup:
                    continue
                if start.departure < now + (margins[place] if boards else 0):
                    continue
                pending.append(("on", trip.id, i + 1, boards + 1, frozenset()))

    # Pairwise Pareto comparison, rather than the router's round-order filter.
    # Boardings break ties only: walking and a direct bus both have 0 transfers.
    def dominates(left, right):
        a, b = left
        c, d = right
        transfers, other_transfers = max(0, b - 1), max(0, d - 1)
        return (a <= c and transfers <= other_transfers
                and (a < c or transfers < other_transfers or b < d))

    return sorted(value for value in arrivals if not any(dominates(other, value) for other in arrivals))


def stop_time(stop, arrival, departure=None, **flags):
    return dict(stop_id=stop, arrival=arrival, departure=arrival if departure is None else departure, **flags)


def trip(identifier, *times, route_id="route"):
    return dict(id=identifier, route_id=route_id, stop_times=times)


def timetable(*trips, stops="ABCD", walks=(), margins=None, service_date="2026-10-10"):
    margins = margins or {}
    return Timetable.model_validate(dict(
        schema_version=1, dataset_id="counterexample", source="synthetic test", is_demo=True,
        service_date=service_date,
        stops=[dict(id=s, name=s, min_transfer_seconds=margins.get(s, 0)) for s in stops],
        trips=trips,
        footpaths=[dict(from_stop=a, to_stop=b, seconds=seconds) for a, b, seconds in walks]))


class JourneyTests(unittest.TestCase):
    def check_route(self, table, request, expected=None):
        result = Router(table).route(request)
        midnight = datetime.combine(table.service_date, datetime.min.time(), timezone(timedelta(hours=8)))
        actual = [(int((j.arrival - midnight).total_seconds()), j.boardings) for j in result.journeys]
        self.assertEqual(actual, oracle(table, request))
        if expected is not None:
            self.assertEqual(actual, expected)
        self.assertEqual(result.status, "ok" if actual else "unreachable")
        margins = {s.id: s.min_transfer_seconds for s in table.stops}
        trips = {t.id: t for t in table.trips}
        for journey in result.journeys:
            self.assertEqual(journey.departure, midnight + timedelta(seconds=request.departure_seconds))
            now, stop, boards = journey.departure, request.origin, 0
            for leg in journey.legs:
                self.assertEqual(leg.from_stop, stop)
                self.assertGreaterEqual(leg.departure, now)
                self.assertGreaterEqual(leg.arrival, leg.departure)
                self.assertLess(leg.arrival - midnight, timedelta(days=2))
                if leg.mode == "walk":
                    self.assertIsNone(leg.trip_id)
                    self.assertIsNone(leg.route_id)
                    self.assertTrue(any(
                        p.from_stop == stop and p.to_stop == leg.to_stop
                        and timedelta(seconds=p.seconds) == leg.arrival - leg.departure
                        for p in table.footpaths))
                else:
                    vehicle = trips[leg.trip_id]
                    self.assertEqual(leg.route_id, vehicle.route_id)
                    self.assertGreaterEqual(leg.departure, now + timedelta(seconds=margins[stop] if boards else 0))
                    self.assertTrue(any(
                        start.stop_id == stop and start.pickup
                        and midnight + timedelta(seconds=start.departure) == leg.departure
                        and end.stop_id == leg.to_stop and end.dropoff
                        and midnight + timedelta(seconds=end.arrival) == leg.arrival
                        for i, start in enumerate(vehicle.stop_times)
                        for end in vehicle.stop_times[i + 1:]))
                    boards += 1
                stop, now = leg.to_stop, leg.arrival
            self.assertEqual(stop, request.destination)
            self.assertEqual(now, journey.arrival)
            self.assertEqual(boards, journey.boardings)
            self.assertEqual(max(0, boards - 1), journey.transfers)
        return result

    def test_faster_direct_bus_dominates_later_walk_at_zero_transfers(self):
        table = timetable(
            trip("direct", stop_time("A", 0), stop_time("D", 20)),
            trip("feeder", stop_time("A", 0), stop_time("B", 5)),
            trip("connection", stop_time("B", 5), stop_time("D", 15)),
            walks=[("A", "D", 30)])
        result = self.check_route(table, query(departure_seconds=0), [(15, 2), (20, 1)])
        self.assertEqual([j.transfers for j in result.journeys], [1, 0])
        self.check_route(table, query(departure_seconds=0, max_transfers=0), [(20, 1)])

    def test_walk_wins_equal_arrival_or_earlier_at_zero_transfers(self):
        for walking_time in (15, 20):
            with self.subTest(walking_time=walking_time):
                table = timetable(trip("direct", stop_time("A", 0), stop_time("D", 20)),
                                  walks=[("A", "D", walking_time)])
                self.check_route(table, query(departure_seconds=0), [(walking_time, 0)])

    def test_earliest_and_fewer_transfer_frontier_and_limit(self):
        table = timetable(
            trip("direct", stop_time("A", 0), stop_time("D", 50)),
            trip("feed", stop_time("A", 0), stop_time("B", 5)),
            trip("middle", stop_time("B", 5), stop_time("C", 10)),
            trip("two", stop_time("B", 5), stop_time("D", 30)),
            trip("three", stop_time("C", 10), stop_time("D", 20)),
            trip("dominated", stop_time("C", 10), stop_time("D", 30)))
        for limit, expected in [(0, [(50, 1)]), (1, [(30, 2), (50, 1)]),
                                (2, [(20, 3), (30, 2), (50, 1)]), (4, [(20, 3), (30, 2), (50, 1)])]:
            with self.subTest(limit=limit):
                self.check_route(table, query(departure_seconds=0, max_transfers=limit), expected)

    def test_earlier_label_can_wait_for_connection(self):
        table = timetable(
            trip("late", stop_time("A", 5), stop_time("B", 9)),
            trip("early", stop_time("A", 0), stop_time("B", 1)),
            trip("connection", stop_time("B", 10), stop_time("D", 20)), margins={"B": 2})
        result = self.check_route(table, query(departure_seconds=0), [(20, 2)])
        self.assertEqual(result.journeys[0].legs[0].trip_id, "early")

    def test_dominated_intermediate_stop_does_not_lose_onboard_continuation(self):
        table = timetable(
            trip("feeder", stop_time("A", 0), stop_time("B", 10)),
            trip("through", stop_time("A", 0), stop_time("B", 20, 30, pickup=False), stop_time("D", 40)),
            margins={"B": 120})
        result = self.check_route(table, query(departure_seconds=0), [(40, 1)])
        self.assertEqual([leg.trip_id for leg in result.journeys[0].legs], ["through"])

    def test_overtaking_does_not_depend_on_input_or_route_order(self):
        slow = trip("slow", stop_time("A", 0), stop_time("B", 10), stop_time("D", 80))
        express = trip("express", stop_time("A", 5), stop_time("B", 15), stop_time("D", 20))
        for services in ((slow, express), (express, slow)):
            with self.subTest(services=services):
                result = self.check_route(timetable(*services), query(departure_seconds=0), [(20, 1)])
                self.assertEqual(result.journeys[0].legs[0].trip_id, "express")

    def test_repeated_stop_boarding_occurrences(self):
        table = timetable(trip("loop", stop_time("A", 0, pickup=False), stop_time("B", 5),
                               stop_time("A", 10), stop_time("D", 20)))
        result = self.check_route(table, query(departure_seconds=5), [(20, 1)])
        self.assertEqual(result.journeys[0].legs[0].departure.second, 10)

    def test_pickup_dropoff_and_through_riding(self):
        table = timetable(
            trip("no-pickup", stop_time("A", 0, pickup=False), stop_time("D", 1)),
            trip("through", stop_time("A", 0), stop_time("B", 5, pickup=False, dropoff=False), stop_time("D", 20)),
            trip("tempting-transfer", stop_time("B", 5), stop_time("D", 10)))
        self.check_route(table, query(departure_seconds=0), [(20, 1)])
        self.check_route(table, query(departure_seconds=0, destination="B"), [])
        self.check_route(table, query(departure_seconds=6, origin="B"), [])

    def test_destination_uses_permitted_arrival_not_departure_or_margin(self):
        table = timetable(trip("loop", stop_time("A", 0), stop_time("D", 5, dropoff=False),
                               stop_time("B", 10), stop_time("D", 20, 100, pickup=False)),
                          margins={"D": 3600})
        self.check_route(table, query(departure_seconds=0), [(20, 1)])
        blocked = timetable(trip("no-dropoff", stop_time("A", 0), stop_time("D", 5, dropoff=False)))
        self.check_route(blocked, query(departure_seconds=0), [])

    def test_chained_access_transfer_and_egress_with_boarding_stop_margin(self):
        for connection_time, expected in [(17, [(25, 2)]), (16, [])]:
            with self.subTest(connection_time=connection_time):
                table = timetable(
                    trip("first", stop_time("C", 5), stop_time("D", 10)),
                    trip("second", stop_time("F", connection_time), stop_time("G", 20)),
                    stops="ABCDEFGHI", margins={"C": 3600, "D": 3600, "F": 2},
                    walks=[("A", "B", 2), ("B", "C", 3), ("D", "F", 20),
                           ("D", "E", 2), ("E", "F", 3), ("E", "D", 1),
                           ("G", "H", 2), ("H", "I", 3)])
                result = self.check_route(table, query(departure_seconds=0, destination="I"), expected)
                if expected:
                    self.assertEqual([leg.mode for leg in result.journeys[0].legs],
                                     ["walk", "walk", "bus", "walk", "walk", "bus", "walk", "walk"])
                self.check_route(table, query(departure_seconds=0, origin="I", destination="A"), [])

    def test_walking_closure_improves_stale_labels_and_preserves_paths(self):
        table = timetable(walks=[("A", "B", 10), ("A", "C", 1), ("C", "B", 1),
                                 ("B", "D", 1), ("B", "C", 1), ("A", "D", 20)])
        result = self.check_route(table, query(departure_seconds=0), [(3, 0)])
        self.assertEqual([leg.to_stop for leg in result.journeys[0].legs], ["C", "B", "D"])
        self.check_route(table, query(origin="D", destination="A", departure_seconds=0), [])

    def test_egress_walk_improves_destination_bus_arrival(self):
        table = timetable(trip("through", stop_time("A", 0), stop_time("B", 10), stop_time("D", 20)),
                          walks=[("B", "C", 2), ("C", "D", 2)])
        result = self.check_route(table, query(departure_seconds=0), [(14, 1)])
        self.assertEqual([leg.to_stop for leg in result.journeys[0].legs], ["B", "C", "D"])

    def test_zero_duration_bus_cycles_and_same_origin_destination(self):
        table = timetable(
            trip("out", stop_time("A", 0), stop_time("A", 0), stop_time("B", 0)),
            trip("back", stop_time("B", 0), stop_time("A", 0)),
            trip("finish", stop_time("B", 0), stop_time("D", 0)))
        self.check_route(table, query(departure_seconds=0, max_transfers=4), [(0, 2)])
        self.check_route(table, query(departure_seconds=0, max_transfers=0), [])
        result = self.check_route(table, query(departure_seconds=0, destination="A"), [(0, 0)])
        self.assertEqual(result.journeys[0].legs, ())

    def test_five_boardings_at_maximum_transfer_limit(self):
        table = timetable(*(trip(str(i), stop_time(a, 0), stop_time(b, 0))
                            for i, (a, b) in enumerate(zip("ABCDE", "BCDEF"))), stops="ABCDEF")
        self.check_route(table, query(departure_seconds=0, destination="F", max_transfers=4), [(0, 5)])
        self.check_route(table, query(departure_seconds=0, destination="F", max_transfers=3), [])

    def test_midnight_transfer_and_next_day_query(self):
        table = timetable(
            trip("night", stop_time("A", 86390), stop_time("B", 86400)),
            trip("next-day", stop_time("B", 86405), stop_time("D", 86420)), margins={"B": 5})
        result = self.check_route(table, query(departure_seconds=86390), [(86420, 2)])
        self.assertEqual(result.journeys[0].arrival.isoformat(), "2026-10-11T00:00:20+08:00")
        self.check_route(table, query(departure_seconds=86401, origin="B"), [(86420, 1)])
        self.check_route(table, query(departure_seconds=86406, origin="B"), [])

    def test_exclusive_48_hour_horizon(self):
        table = timetable(walks=[("A", "B", 1), ("B", "D", 1)])
        self.check_route(table, query(departure_seconds=172798, destination="B"), [(172799, 0)])
        self.check_route(table, query(departure_seconds=172798), [])
        self.check_route(table, query(departure_seconds=172799, destination="A"), [(172799, 0)])
        table = timetable(trip("last", stop_time("A", 172799), stop_time("D", 172799)))
        self.check_route(table, query(departure_seconds=172799), [(172799, 1)])

    def test_snapshot_date_must_allow_representable_overnight_timestamps(self):
        with self.assertRaises(ValidationError):
            timetable(service_date="9999-12-31")
        table = timetable(trip("last", stop_time("A", 172799), stop_time("D", 172799)),
                          service_date="9999-12-30")
        result = self.check_route(table, query(service_date="9999-12-30", departure_seconds=172799), [(172799, 1)])
        self.assertEqual(result.journeys[0].arrival.isoformat(), "9999-12-31T23:59:59+08:00")

    def test_demo_tradeoffs_walking_and_through_ride(self):
        result = engine.route(query())
        self.assertEqual([(j.arrival.hour, j.arrival.minute, j.transfers) for j in result.journeys],
                         [(8, 25, 1), (8, 45, 0)])
        self.assertEqual([leg.mode for leg in result.journeys[0].legs], ["bus", "walk", "bus"])
        self.assertEqual(len(result.journeys[1].legs), 1)
        self.assertEqual(engine.route(query(max_transfers=0)).journeys[0].arrival.minute, 45)

    def test_missed_transfer_by_one_second(self):
        data = engine.timetable.model_dump()
        data["stops"] = [s.model_copy(update={"min_transfer_seconds": 61}) if s.id == "C" else s
                         for s in engine.timetable.stops]
        result = Router(Timetable.model_validate(data)).route(query())
        self.assertEqual(len(result.journeys), 1)
        self.assertEqual(result.journeys[0].arrival.minute, 45)

    def test_midnight_late_departure_and_unreachable(self):
        night = engine.route(query(departure_seconds=28801)).journeys[0]
        self.assertEqual(night.arrival.date(), date(2026, 10, 11))
        self.assertEqual(night.arrival.hour, 0)
        self.assertEqual(engine.route(query(departure_seconds=85801)).status, "unreachable")
        self.assertEqual(engine.route(query(destination="X")).journeys, ())
        self.assertEqual(engine.route(query(origin="B", destination="C")).journeys[0].boardings, 0)
        self.assertEqual(engine.route(query(destination="A")).journeys[0].legs, ())

    def test_validation(self):
        for changes in ({"departure_seconds": -1}, {"departure_seconds": 172800},
                        {"max_transfers": 5}, {"departure_seconds": True}, {"surprise": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                query(**changes)
        for changes in ({"origin": "missing"}, {"service_date": "2026-10-11"}):
            with self.assertRaises(ValueError):
                engine.route(query(**changes))
        data = engine.timetable.model_dump(mode="json")
        data["trips"][0]["stop_times"][1]["arrival"] = 1
        with self.assertRaises(ValidationError):
            Timetable.model_validate(data)
        data = engine.timetable.model_dump(mode="json")
        data["footpaths"][0]["to_stop"] = "missing"
        with self.assertRaises(ValidationError):
            Timetable.model_validate(data)

    def test_validation_of_durations_ids_and_references(self):
        for seconds in (0, -1, True, 1.5, 7201):
            with self.subTest(seconds=seconds), self.assertRaises(ValidationError):
                timetable(walks=[("A", "D", seconds)])
        for arrival in (-1, 172800, True, 0.5):
            with self.subTest(arrival=arrival), self.assertRaises(ValidationError):
                timetable(trip("invalid", stop_time("A", 0), stop_time("D", arrival)))
        for table_args in (
            dict(stops="AABC"),
            dict(walks=[("A", "missing", 1)]),
            dict(margins={"A": -1}),
        ):
            with self.subTest(table_args=table_args), self.assertRaises(ValidationError):
                timetable(**table_args)
        for services in (
            [trip("unknown", stop_time("A", 0), stop_time("missing", 1))],
            [trip("duplicate", stop_time("A", 0), stop_time("D", 1))] * 2,
            [trip("backwards-dwell", stop_time("A", 1, 0), stop_time("D", 2))],
            [trip("backwards-leg", stop_time("A", 0, 2), stop_time("D", 1))],
            [trip("unnormalized-midnight", stop_time("A", 86399), stop_time("D", 1))],
        ):
            with self.subTest(services=services), self.assertRaises(ValidationError):
                timetable(*services)

    def test_exhaustive_oracle_random_small_timetables(self):
        rng = random.Random(761)
        for case in range(100):
            base = (0, 86390, 172740)[case % 3]
            trips = []
            for t in range(5):
                now = base + rng.randrange(0, 15)
                times = []
                for stop in rng.choices("ABCD", k=rng.randrange(2, 5)):
                    now += rng.randrange(0, 6)
                    departure = now + rng.randrange(0, 3)
                    times.append(dict(stop_id=stop, arrival=now, departure=departure,
                                      pickup=rng.random() > .25, dropoff=rng.random() > .25))
                    now = departure
                trips.append(dict(id=str(t), route_id="route", stop_times=times))
            table = Timetable.model_validate(dict(
                schema_version=1, dataset_id="oracle", source="generated", is_demo=True,
                service_date="2026-10-10",
                stops=[dict(id=s, name=s, min_transfer_seconds=rng.randrange(0, 4)) for s in "ABCD"],
                trips=trips, footpaths=[dict(from_stop=a, to_stop=b, seconds=rng.randrange(1, 6))
                                       for a in "ABCD" for b in "ABCD" if a != b and rng.random() < .25]))
            for offset, limit in zip((0, 10, 25, 50), (0, 1, 2, 4)):
                request = query(departure_seconds=base + offset, max_transfers=limit,
                                origin=rng.choice("ABCD"), destination=rng.choice("ABCD"))
                with self.subTest(case=case, request=request):
                    self.check_route(table, request)

    def test_shared_router_keeps_query_state_local(self):
        requests = [query(departure_seconds=start, max_transfers=limit, destination=destination)
                    for start in (28800, 28801, 85800, 172799)
                    for limit in (0, 2, 4) for destination in ("A", "D", "X")]
        expected = [engine.route(request) for request in requests]
        with ThreadPoolExecutor(max_workers=8) as pool:
            actual = list(pool.map(engine.route, requests))
        self.assertEqual(actual, expected)

    def test_api(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_journey_router] = lambda: engine
        with TestClient(app) as client:
            body = query().model_dump(mode="json")
            response = client.post("/api/v1/journeys", json=body)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["is_demo"])
            self.assertEqual(client.post("/api/v1/journeys", json=body | {"origin": "?"}).status_code, 422)
            self.assertEqual(client.post("/api/v1/journeys", json=body | {"max_transfers": -1}).status_code, 422)
            self.assertEqual(client.post("/api/v1/journeys", json=body | {"destination": "X"}).json()["status"], "unreachable")

    def test_unconfigured_api_and_main_registration(self):
        from app.main import app as main_app

        self.assertIn("/api/v1/journeys", main_app.openapi()["paths"])
        # No lifespan: this exercises real registration/middleware without
        # starting database initialization and the static sync scheduler.
        with patch.dict(main_app.dependency_overrides, {get_journey_router: lambda: engine}):
            response = TestClient(main_app).post("/api/v1/journeys", json=query().model_dump(mode="json"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["dataset_id"], engine.timetable.dataset_id)
        app = FastAPI()
        app.include_router(router)
        _load_configured_router.cache_clear()
        try:
            with patch.dict("os.environ", {"JOURNEY_TIMETABLE_PATH": ""}), TestClient(app) as client:
                response = client.post("/api/v1/journeys", json=query().model_dump(mode="json"))
                self.assertEqual(response.status_code, 503)
            with patch("app.api.journeys.load_router", side_effect=ValueError("private path")):
                path = str(Path(__file__).resolve().parent / "invalid.json")
                with patch.dict("os.environ", {"JOURNEY_TIMETABLE_PATH": path}), TestClient(app) as client:
                    response = client.post("/api/v1/journeys", json=query().model_dump(mode="json"))
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn("private path", response.text)
        finally:
            _load_configured_router.cache_clear()


class JourneyLoaderTests(unittest.TestCase):
    def setUp(self):
        _load_configured_router.cache_clear()
        self.addCleanup(_load_configured_router.cache_clear)
        directory = tempfile.TemporaryDirectory(prefix="journey-loader-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "private timetable.json"
        environment = patch.dict("os.environ", {"JOURNEY_TIMETABLE_PATH": str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def request(self):
        return self.client.post("/api/v1/journeys", json=query().model_dump(mode="json"))

    def assert_unavailable(self, response):
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(str(self.directory), response.text)
        self.assertNotIn(self.path.name, response.text)
        self.assertNotIn("private error", response.text)

    def test_actual_file_is_validated_served_and_cached_until_restart(self):
        self.path.write_text(engine.timetable.model_dump_json(), encoding="utf-8")
        first = self.request()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), engine.route(query()).model_dump(mode="json"))
        loaded = get_journey_router()
        self.path.write_text("invalid replacement", encoding="utf-8")
        with patch.dict("os.environ", {"JOURNEY_TIMETABLE_PATH": ""}):
            self.assertIs(get_journey_router(), loaded)
            self.assertEqual(self.request().json(), first.json())

    def test_configured_path_must_be_absolute(self):
        for path in ("app/data/journey-demo-v1.json", " ", "./timetable.json"):
            with self.subTest(path=path):
                _load_configured_router.cache_clear()
                with patch.dict("os.environ", {"JOURNEY_TIMETABLE_PATH": path}):
                    with patch("app.api.journeys.load_router", return_value=engine) as load:
                        self.assert_unavailable(self.request())
                        load.assert_not_called()

    def test_missing_and_invalid_files_are_retryable(self):
        self.assert_unavailable(self.request())
        self.path.write_text("private error: invalid JSON", encoding="utf-8")
        self.assert_unavailable(self.request())
        self.path.write_text(engine.timetable.model_dump_json(), encoding="utf-8")
        self.assertEqual(self.request().status_code, 200)

    def test_encoding_schema_and_directory_errors_are_503(self):
        for content in (b"\xff", b"{}", b'{"private error": true}'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assert_unavailable(self.request())
        with patch.dict("os.environ", {"JOURNEY_TIMETABLE_PATH": str(self.directory)}):
            self.assert_unavailable(self.request())
        for error in (PermissionError("private error"), OSError("private error"), ValueError("private error")):
            with self.subTest(error=error), patch("app.api.journeys.load_router", side_effect=error):
                self.assert_unavailable(self.request())

    def test_concurrent_cold_requests_load_one_snapshot(self):
        workers = 8
        ready = threading.Barrier(workers + 1)
        entered, duplicate, release = threading.Event(), threading.Event(), threading.Event()
        calls_lock = threading.Lock()
        calls = 0

        def slow_load(path):
            nonlocal calls
            self.assertEqual(path, self.path)
            with calls_lock:
                calls += 1
                if calls > 1:
                    duplicate.set()
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test loader was not released")
            return engine

        def fetch():
            ready.wait(5)
            return get_journey_router()

        with patch("app.api.journeys.load_router", side_effect=slow_load) as load:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(fetch) for _ in range(workers)]
                try:
                    ready.wait(5)
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(duplicate.wait(.5), "cold requests loaded multiple snapshots")
                finally:
                    release.set()
                self.assertTrue(all(future.result(timeout=5) is engine for future in futures))
            load.assert_called_once_with(self.path)


if __name__ == "__main__":
    unittest.main()
