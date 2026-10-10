"""Offline GTFS -> fixed-date journey snapshot. Run python -m app.journey_gtfs --help."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Annotated, Literal
import zipfile

from pydantic import Field

from app.journeys import HORIZON_SECONDS, Model, Timetable

IMPORTER_VERSION = "journey-gtfs-v1"
SPEC_URL = "https://gtfs.org/documentation/schedule/reference/"
DAY = 86400
# The router uses fixed UTC+08:00, not historical Taipei daylight saving time.
FIRST_SERVICE_DATE = date(1980, 1, 1)
MAX_TABLE_BYTES = 128 * 1024 * 1024
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_TRIP_SPAN = 7 * DAY
BUS_TYPES = {3, 11, 800, *range(200, 210), *range(700, 717)}
RESTRICTED_BUS_TYPES = {707, 708, 709, 712, 715}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TABLES = {
    "agency.txt": ("agency_name", "agency_url", "agency_timezone"),
    "stops.txt": ("stop_id",),
    "routes.txt": ("route_id", "route_type"),
    "trips.txt": ("route_id", "service_id", "trip_id"),
    "stop_times.txt": ("trip_id", "stop_sequence"),
    "calendar.txt": ("service_id", *WEEKDAYS, "start_date", "end_date"),
    "calendar_dates.txt": ("service_id", "date", "exception_type"),
    "frequencies.txt": ("trip_id", "start_time", "end_time", "headway_secs"),
    "transfers.txt": (),
    "feed_info.txt": ("feed_publisher_name", "feed_publisher_url", "feed_lang"),
}
REQUIRED_TABLES = {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}


class InputError(ValueError):
    def __init__(self, code: str, message: str, **context):
        super().__init__(message)
        self.issue = {"code": code, "message": message, **context}


Margin = Annotated[int, Field(strict=True, ge=0, le=3600)]


class WalkingLink(Model):
    from_stop: Annotated[str, Field(min_length=1)]
    to_stop: Annotated[str, Field(min_length=1)]
    seconds: Annotated[int, Field(strict=True, gt=0, le=7200)]


class WalkingConfig(Model):
    schema_version: Literal[1] = 1
    default_min_transfer_seconds: Margin = 120
    min_transfer_seconds: dict[str, Margin] = Field(default_factory=dict)
    footpaths: tuple[WalkingLink, ...] = ()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_id(feed_id: str, kind: str, *parts: str) -> str:
    """IDs do not depend on CSV ordering, ZIP metadata, or the requested date.

    A run includes its ORIGINAL service date, distinguishing daily repetitions
    while preserving its identity in overlapping snapshots.
    """
    return f"gtfs:{feed_id}:{kind}:{digest(json_bytes(parts))}"


def required(row: dict, field: str) -> str:
    value = row.get(field, "")
    if not value:
        raise InputError("missing_value", f"Required value {field} is empty")
    return value


def integer(value: str, field: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise InputError("invalid_integer", f"{field} must be a nonnegative integer")
    return int(value)


def enum(row: dict, field: str, values: set[int], default: str = "") -> int:
    result = integer(row.get(field, "") or default, field)
    if result not in values:
        raise InputError("invalid_enum", f"Unsupported {field} value {result}")
    return result


def gtfs_date(value: str) -> date:
    if not re.fullmatch(r"[0-9]{8}", value):
        raise InputError("invalid_date", "GTFS dates must use YYYYMMDD")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError as exc:
        raise InputError("invalid_date", "Invalid GTFS calendar date") from exc


def gtfs_time(value: str) -> int:
    match = re.fullmatch(r"([0-9]{1,6}):([0-5][0-9]):([0-5][0-9])", value)
    if not match:
        raise InputError("invalid_time", "Expected exact H:MM:SS or HH:MM:SS (hours may exceed 24)")
    hours, minutes, seconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _parse_csv(name: str, raw: bytes) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        fields = reader.fieldnames
        if not fields or any(not field or field != field.strip() for field in fields) or len(set(fields)) != len(fields):
            raise InputError("invalid_header", "Empty, padded, or duplicate CSV column", file=name)
        missing = set(TABLES[name]) - set(fields)
        if missing:
            raise InputError("missing_columns", f"Missing columns: {', '.join(sorted(missing))}", file=name)
        rows = []
        for row in reader:
            if None in row or None in row.values():
                raise InputError("invalid_csv_row", "CSV row has the wrong number of fields", file=name, row=reader.line_num)
            if any(any(c in value for c in "\r\n\t\x00") for value in row.values()):
                raise InputError("invalid_csv_value", "GTFS values cannot contain newlines, tabs, or NUL", file=name, row=reader.line_num)
            rows.append(row)
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise InputError("invalid_csv", "Invalid UTF-8/CSV input", file=name) from exc


def read_feed(path: Path, report: dict) -> dict[str, list[dict]]:
    """Read only the named GTFS tables; never extract a ZIP or open .env files."""
    tables = {}
    total = 0

    def consume(name: str, stream, size: int):
        nonlocal total
        if size > MAX_TABLE_BYTES or total + size > MAX_INPUT_BYTES:
            raise InputError("input_limit", "GTFS input exceeds the documented import size limit", file=name)
        raw = stream.read(MAX_TABLE_BYTES + 1)
        total += len(raw)
        if len(raw) > MAX_TABLE_BYTES or total > MAX_INPUT_BYTES:
            raise InputError("input_limit", "GTFS input exceeds the documented import size limit", file=name)
        tables[name] = _parse_csv(name, raw)
        report["input"]["tables"][name] = {"sha256": digest(raw), "bytes": len(raw), "rows": len(tables[name])}

    if path.is_dir():
        root = path.resolve()
        entries = {item.name: item for item in root.iterdir()}
        report["input"]["format"] = "directory"
        report["input"]["ignored_entries"] = sorted(set(entries) - set(TABLES))
        for name in sorted(set(entries) & set(TABLES)):
            item = entries[name]
            if item.is_symlink() or item.resolve().parent != root or not item.is_file():
                raise InputError("unsafe_input", "GTFS tables must be regular files inside the input directory", file=name)
            with item.open("rb") as stream:
                consume(name, stream, item.stat().st_size)
    else:
        report["input"]["format"] = "zip"
        with zipfile.ZipFile(path) as archive:
            entries = {}
            for info in archive.infolist():
                name = info.filename
                parts = PurePosixPath(name)
                if "\\" in name or parts.is_absolute() or ".." in parts.parts or ":" in name:
                    raise InputError("unsafe_zip_member", "Unsafe ZIP member path", file=name)
                if info.is_dir():
                    continue
                if name in entries:
                    raise InputError("duplicate_zip_member", "Duplicate ZIP member", file=name)
                if parts.name in TABLES and name != parts.name:
                    raise InputError("nested_gtfs_table", "GTFS tables must be at the ZIP root", file=name)
                entries[name] = info
            report["input"]["ignored_entries"] = sorted(set(entries) - set(TABLES))
            for name in sorted(set(entries) & set(TABLES)):
                info = entries[name]
                if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                    raise InputError("unsafe_zip_member", "Symlink/encrypted GTFS tables are not supported", file=name)
                with archive.open(info) as stream:
                    consume(name, stream, info.file_size)
    missing = REQUIRED_TABLES - set(tables)
    if missing:
        raise InputError("missing_tables", f"Missing required tables: {', '.join(sorted(missing))}")
    if not ({"calendar.txt", "calendar_dates.txt"} & set(tables)):
        raise InputError("missing_calendar", "calendar.txt or calendar_dates.txt is required")
    report["input"]["routing_inputs_sha256"] = digest(json_bytes({
        name: entry["sha256"] for name, entry in report["input"]["tables"].items()}))
    return tables


def indexed(rows: list[dict], key: str, file: str) -> dict[str, dict]:
    result = {}
    for number, row in enumerate(rows, 2):
        value = required(row, key)
        if value in result:
            raise InputError("duplicate_id", f"Duplicate {key}", file=file, row=number, id=value)
        result[value] = row
    return result


def calendars(tables: dict):
    weekly = {}
    for key, row in indexed(tables.get("calendar.txt", []), "service_id", "calendar.txt").items():
        start, end = gtfs_date(required(row, "start_date")), gtfs_date(required(row, "end_date"))
        if end < start:
            raise InputError("invalid_calendar_range", "Calendar end precedes start", service_id=key)
        weekly[key] = (start, end, tuple(enum(row, day, {0, 1}) for day in WEEKDAYS))
    exceptions = {}
    for row in tables.get("calendar_dates.txt", []):
        key = (required(row, "service_id"), gtfs_date(required(row, "date")))
        if key in exceptions:
            raise InputError("duplicate_exception", "Duplicate service_id/date calendar exception", service_id=key[0], date=key[1].isoformat())
        exceptions[key] = enum(row, "exception_type", {1, 2})

    def active(service_id: str, day: date) -> bool:
        exception = exceptions.get((service_id, day))
        if exception is not None:
            return exception == 1
        entry = weekly.get(service_id)
        return bool(entry and entry[0] <= day <= entry[1] and entry[2][day.weekday()])

    return set(weekly) | {key[0] for key in exceptions}, active


def validate_locations(stops: dict) -> dict[str, set[str]]:
    ancestors = {}
    for stop_id, row in stops.items():
        location_type = enum(row, "location_type", {0, 1, 2, 3, 4}, "0")
        if location_type in {0, 1, 2}:
            required(row, "stop_name")
            for coordinate, bound in (("stop_lat", 90), ("stop_lon", 180)):
                try:
                    value = float(required(row, coordinate))
                except ValueError as exc:
                    raise InputError("invalid_coordinate", f"Invalid {coordinate}", stop_id=stop_id) from exc
                if not -bound <= value <= bound:
                    raise InputError("invalid_coordinate", f"Invalid {coordinate}", stop_id=stop_id)
        chain = {stop_id}
        parent = row.get("parent_station", "")
        if location_type == 1 and parent or location_type in {2, 3, 4} and not parent:
            raise InputError("invalid_parent_station", "Invalid parent_station for this location type", stop_id=stop_id)
        if parent:
            expected_type = 0 if location_type == 4 else 1
            if parent not in stops or enum(stops[parent], "location_type", set(range(5)), "0") != expected_type:
                raise InputError("invalid_parent_station", "Parent has the wrong location type or is unknown", stop_id=stop_id)
        while parent:
            if parent not in stops or parent in chain:
                raise InputError("invalid_parent_station", "Unknown/cyclic parent_station", stop_id=stop_id)
            chain.add(parent)
            parent = stops[parent].get("parent_station", "")
        ancestors[stop_id] = chain
    return ancestors


def transfer_exclusions(rows: list[dict], trips: dict, routes: dict, stops: dict, times: dict, ancestors: dict, report: dict):
    """Remove BOTH sides of unsupported rules rather than permit a forbidden link.

    Trip selectors take precedence for scoping; a route selector conservatively
    removes that whole route; otherwise stop/station selectors remove all trips
    visiting that location or descendants. This intentionally over-excludes.
    Malformed/unresolvable scopes reject the feed instead of ignoring a rule.
    """
    excluded = defaultdict(list)
    visits = {key: set().union(*(ancestors.get(r.get("stop_id"), set()) for r in values))
              for key, values in times.items()}
    for number, row in enumerate(rows, 2):
        kind = enum(row, "transfer_type", set(range(6)), "0")
        if kind == 2:
            integer(required(row, "min_transfer_time"), "min_transfer_time")
        affected = set()
        for side in ("from", "to"):
            stop_id, route_id, trip_id = (row.get(f"{side}_{field}", "") for field in ("stop_id", "route_id", "trip_id"))
            if (kind < 4 and not stop_id) or (kind >= 4 and not trip_id):
                raise InputError("incomplete_transfer_rule", "Cannot safely scope transfer rule", file="transfers.txt", row=number)
            for value, collection in ((stop_id, stops), (route_id, routes), (trip_id, trips)):
                if value and value not in collection:
                    raise InputError("unknown_transfer_reference", "Transfer rule references an unknown ID", file="transfers.txt", row=number)
            if stop_id and enum(stops[stop_id], "location_type", {0, 1, 2, 3, 4}, "0") not in ({0, 1} if kind < 4 else {0}):
                raise InputError("invalid_transfer_location", "Transfer rule must reference a stop/platform or station", file="transfers.txt", row=number)
            if trip_id and route_id and trips[trip_id]["route_id"] != route_id:
                raise InputError("conflicting_transfer_scope", "Transfer trip and route selectors disagree", file="transfers.txt", row=number)
            if trip_id:
                affected.add(trip_id)
            elif route_id:
                affected.update(key for key, trip in trips.items() if trip["route_id"] == route_id)
            else:
                affected.update(key for key in trips if stop_id in visits.get(key, set()))
        report["transfer_rules"].append({"row": number, "rule": row, "policy": "exclude_both_scopes", "affected_trip_ids": sorted(affected)})
        for key in affected:
            excluded[key].append(number)
    return excluded


def exact_times(rows: list[dict], route: dict, stops: dict) -> list[dict]:
    if len(rows) < 2:
        raise InputError("incomplete_stop_times", "A trip needs at least two complete stop times")
    events = []
    sequences = set()
    for row in rows:
        sequence = integer(required(row, "stop_sequence"), "stop_sequence")
        if sequence in sequences:
            raise InputError("duplicate_stop_sequence", "Duplicate stop_sequence")
        sequences.add(sequence)
        if any(row.get(field) for field in ("location_group_id", "location_id", "start_pickup_drop_off_window",
                                           "end_pickup_drop_off_window", "pickup_booking_rule_id", "drop_off_booking_rule_id")):
            raise InputError("flex_service", "Flexible/booking-window service is not an exact fixed-stop timetable")
        stop_id = required(row, "stop_id")
        if stop_id not in stops or enum(stops[stop_id], "location_type", set(range(5)), "0") != 0:
            raise InputError("invalid_trip_stop", "stop_times must reference an existing stop/platform", stop_id=stop_id)
        if enum(row, "timepoint", {0, 1}, "1") != 1:
            raise InputError("inexact_timepoint", "timepoint=0 denotes approximate/interpolated times")
        if not row.get("arrival_time") or not row.get("departure_time"):
            raise InputError("incomplete_stop_times", "Both arrival_time and departure_time are required at every stop")
        pickup = enum(row, "pickup_type", {0, 1, 2, 3}, "0")
        dropoff = enum(row, "drop_off_type", {0, 1, 2, 3}, "0")
        if pickup > 1 or dropoff > 1:
            raise InputError("conditional_boarding", "Phone/driver-arranged pickup or dropoff is unsupported")
        for field in ("continuous_pickup", "continuous_drop_off"):
            if enum(row, field, {0, 1, 2, 3}, route.get(field) or "1") != 1:
                raise InputError("continuous_boarding", "Continuous pickup/dropoff is unsupported")
        events.append(dict(stop_id=stop_id, arrival=gtfs_time(row["arrival_time"]),
                           departure=gtfs_time(row["departure_time"]), pickup=pickup == 0, dropoff=dropoff == 0,
                           sequence=sequence))
    events.sort(key=lambda event: event["sequence"])
    previous = -1
    for event in events:
        if not previous <= event["arrival"] <= event["departure"]:
            raise InputError("nonmonotonic_times", "Stop times must be monotonic; overnight times must already exceed 24:00:00")
        previous = event["departure"]
    if events[-1]["departure"] - events[0]["arrival"] > MAX_TRIP_SPAN:
        raise InputError("trip_span_limit", "Trips spanning more than seven days are unsupported")
    return events


def import_gtfs(input_path: str | Path, *, service_date: date, feed_id: str, source: str,
                synthetic: bool = False, walking_config: str | Path | None = None) -> tuple[Timetable | None, dict]:
    """Return a validated snapshot and a loss/provenance report; no writes/network."""
    report = {
        "report_schema_version": 1, "importer": IMPORTER_VERSION, "specification": SPEC_URL,
        "status": "rejected", "request": {"service_date": service_date.isoformat(), "feed_id": feed_id,
                                          "source": source, "is_demo": synthetic},
        "input": {"tables": {}}, "fatal_errors": [], "excluded_trips": [], "omitted_runs": [],
        "not_selected": [], "imported_runs": [], "transfer_rules": [], "warnings": [],
        "assumptions": ["No geographic, parent-station, pathways.txt, or transfers.txt walking links are inferred.",
                        "Same-stop transfers use the configured boarding-stop margin; default is 120 seconds.",
                        "No realtime, fares, accessibility, vehicle-block interlining, or publisher verification."],
    }
    try:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", feed_id):
            raise InputError("invalid_feed_id", "feed-id must be 1..32 ASCII letters/digits, dot, underscore, or hyphen")
        if not source.strip():
            raise InputError("missing_source", "A nonempty provenance/source description is required")
        if not FIRST_SERVICE_DATE <= service_date <= date(9999, 12, 30):
            raise InputError("unsupported_snapshot_date", "Snapshot date must be 1980-01-01 through 9999-12-30")
        config = WalkingConfig()
        if walking_config is not None:
            raw = Path(walking_config).read_bytes()
            config = WalkingConfig.model_validate_json(raw)
            report["walking_config_sha256"] = digest(raw)
        report["walking_config"] = config.model_dump(mode="json")
        tables = read_feed(Path(input_path), report)
        agencies = tables["agency.txt"]
        if not agencies or any(required(row, "agency_timezone") != "Asia/Taipei" for row in agencies):
            raise InputError("unsupported_timezone", "Every agency must explicitly use agency_timezone=Asia/Taipei")
        for row in agencies:
            required(row, "agency_name")
            required(row, "agency_url")
        agency_ids = {row.get("agency_id", "") for row in agencies}
        if len(agency_ids) != len(agencies) or (len(agencies) > 1 and "" in agency_ids):
            raise InputError("invalid_agency_ids", "Multiple agencies require distinct nonempty agency_id values")
        report["agencies"] = agencies
        report["feed_info"] = tables.get("feed_info.txt", [])
        stops = indexed(tables["stops.txt"], "stop_id", "stops.txt")
        if not stops:
            raise InputError("no_stops", "No stops are available")
        ancestors = validate_locations(stops)
        routes = indexed(tables["routes.txt"], "route_id", "routes.txt")
        route_types = {}
        for key, row in routes.items():
            route_types[key] = integer(required(row, "route_type"), "route_type")
            if not row.get("route_short_name") and not row.get("route_long_name"):
                raise InputError("missing_route_name", "A route short or long name is required", route_id=key)
            agency_id = row.get("agency_id", "")
            if (agency_id and agency_id not in agency_ids) or (len(agencies) > 1 and not agency_id):
                raise InputError("unknown_agency", "Route agency_id is missing or unknown", route_id=key)
        trips = indexed(tables["trips.txt"], "trip_id", "trips.txt")
        service_ids, active = calendars(tables)
        for key, row in trips.items():
            if required(row, "route_id") not in routes or required(row, "service_id") not in service_ids:
                raise InputError("unknown_trip_reference", "Trip route_id/service_id is unknown", trip_id=key)
        times = defaultdict(list)
        for row in tables["stop_times.txt"]:
            key = required(row, "trip_id")
            if key not in trips:
                raise InputError("unknown_trip_reference", "stop_times references an unknown trip", trip_id=key)
            times[key].append(row)
        frequencies = defaultdict(list)
        for number, row in enumerate(tables.get("frequencies.txt", []), 2):
            key = required(row, "trip_id")
            if key not in trips:
                raise InputError("unknown_trip_reference", "frequencies references an unknown trip", trip_id=key)
            frequencies[key].append({"row": number, "exact_times": row.get("exact_times", "") or "0"})
        transfer_trips = transfer_exclusions(tables.get("transfers.txt", []), trips, routes, stops, times, ancestors, report)
        stop_ids = {key: stable_id(feed_id, "s", key) for key in stops}
        route_ids = {key: stable_id(feed_id, "r", key) for key in routes}
        report["ids"] = {"stops": stop_ids, "routes": route_ids}
        if any(key not in stops for key in config.min_transfer_seconds) or any(
                link.from_stop not in stops or link.to_stop not in stops for link in config.footpaths):
            raise InputError("unknown_walking_stop", "Walking configuration must reference original GTFS stop IDs")
        output_trips = []
        for key, trip in sorted(trips.items()):
            route_id = trip["route_id"]
            if route_types[route_id] not in BUS_TYPES:
                report["not_selected"].append({"trip_id": key, "reason": "non_bus_route", "route_type": route_types[route_id]})
                continue
            problems = []
            if route_types[route_id] in RESTRICTED_BUS_TYPES:
                problems.append({"code": "restricted_bus_type", "message": "Demand-responsive/restricted rider service is unsupported"})
            if key in frequencies:
                problems.append({"code": "frequency_service", "message": "Frequency templates (including exact_times=1) are not expanded", "frequency_rows": frequencies[key]})
            if key in transfer_trips:
                problems.append({"code": "unsupported_transfer_rule", "message": "Trip is in a conservatively excluded transfer-rule scope", "transfer_rows": transfer_trips[key]})
            try:
                events = exact_times(times[key], routes[route_id], stops)
            except InputError as exc:
                problems.append(exc.issue)
            if problems:
                report["excluded_trips"].append({"trip_id": key, "reasons": problems})
                continue
            if trip.get("block_id"):
                report["warnings"].append({"code": "block_not_interlined", "trip_id": key, "block_id": trip["block_id"]})
            # Consider every possibly overlapping service day, not just D-1/D/D+1.
            # Thus e.g. 49:00 service from D-2 is included. Absolute HH offsets
            # up to six digits are supported; event span is bounded to seven days.
            first = service_date.toordinal() - events[-1]["departure"] // DAY
            last = service_date.toordinal() + 1 - events[0]["arrival"] // DAY
            selected = 0
            for ordinal in range(max(1, first), min(date.max.toordinal(), last) + 1):
                day = date.fromordinal(ordinal)
                if not active(trip["service_id"], day):
                    continue
                selected += 1
                run = {"trip_id": key, "service_date": day.isoformat()}
                if day < FIRST_SERVICE_DATE:
                    report["omitted_runs"].append(run | {"reason": "historical_timezone_unsupported"})
                    continue
                shift = (day - service_date).days * DAY
                kept, discarded = [], []
                for event in events:
                    arrival, departure = event["arrival"] + shift, event["departure"] + shift
                    if 0 <= arrival <= departure < HORIZON_SECONDS:
                        kept.append(dict(stop_id=stop_ids[event["stop_id"]], arrival=arrival, departure=departure,
                                         pickup=event["pickup"], dropoff=event["dropoff"]))
                    else:
                        discarded.append({"stop_sequence": event["sequence"], "stop_id": event["stop_id"],
                                          "arrival": arrival, "departure": departure,
                                          "straddles_boundary": arrival < 0 <= departure or arrival < HORIZON_SECONDS <= departure})
                if len(kept) < 2:
                    report["omitted_runs"].append(run | {"reason": "fewer_than_two_in_horizon_events", "discarded_events": discarded})
                    continue
                identifier = stable_id(feed_id, "t", key, day.isoformat())
                output_trips.append(dict(id=identifier, route_id=route_ids[route_id], stop_times=kept))
                report["imported_runs"].append(run | {"id": identifier, "stop_times": len(kept), "discarded_events": discarded})
            if not selected:
                report["not_selected"].append({"trip_id": key, "reason": "inactive_in_overlapping_service_dates"})
        snapshot_key = digest(json_bytes({"importer": IMPORTER_VERSION, "request": report["request"],
                                         "inputs": report["input"]["routing_inputs_sha256"],
                                         "walking_config": report["walking_config"]}))
        summary = {"input_trips": len(trips), "imported_runs": len(output_trips),
                   "excluded_trip_templates": len(report["excluded_trips"]), "not_selected_templates": len(report["not_selected"]),
                   "omitted_runs": len(report["omitted_runs"]),
                   "clipped_runs": sum(bool(run["discarded_events"]) for run in report["imported_runs"]),
                   "stops": len(stops), "footpaths": len(config.footpaths)}
        report["summary"] = summary
        report["exclusion_reason_counts"] = dict(Counter(reason["code"] for trip in report["excluded_trips"] for reason in trip["reasons"]))
        report["status"] = "partial" if summary["excluded_trip_templates"] or summary["omitted_runs"] or summary["clipped_runs"] else "ok"
        timetable = Timetable.model_validate(dict(
            schema_version=1, dataset_id=f"gtfs:{feed_id}:{service_date.isoformat()}:{snapshot_key[:16]}",
            source=f"{source}; {IMPORTER_VERSION}; import={snapshot_key}; status={report['status']}; excluded_templates={summary['excluded_trip_templates']}; omitted_runs={summary['omitted_runs']}; clipped_runs={summary['clipped_runs']}",
            is_demo=synthetic, service_date=service_date, timezone="Asia/Taipei",
            stops=[dict(id=stop_ids[key], name=row.get("stop_name") or key,
                        min_transfer_seconds=config.min_transfer_seconds.get(key, config.default_min_transfer_seconds))
                   for key, row in sorted(stops.items())],
            trips=output_trips,
            footpaths=[dict(from_stop=stop_ids[p.from_stop], to_stop=stop_ids[p.to_stop], seconds=p.seconds) for p in config.footpaths]))
        report["dataset_id"] = timetable.dataset_id
        report["timetable_sha256"] = digest(json_bytes(timetable.model_dump(mode="json")))
        return timetable, report
    except InputError as exc:
        report["fatal_errors"].append(exc.issue)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        report["fatal_errors"].append({"code": "invalid_input", "message": str(exc)})
    report["status"] = "rejected"
    return None, report


def validate_output_paths(input_path: Path, config_path: Path | None, output: Path, report: Path) -> None:
    """No clobber, including symlinks, hardlinks, input aliases and feed children."""
    source = input_path.resolve()
    targets = [output.resolve(), report.resolve()]
    if targets[0] == targets[1]:
        raise ValueError("Timetable and report paths must differ")
    for path, target in zip((output, report), targets):
        if target == source or (config_path is not None and target == config_path.resolve()):
            raise ValueError("Output paths must not alias an input")
        if input_path.is_dir() and target.is_relative_to(source):
            raise ValueError("Outputs must be outside the source GTFS directory")
        if path.exists() or path.is_symlink():
            raise ValueError(f"Output already exists; choose a new path: {path}")
        if not path.parent.is_dir():
            raise ValueError(f"Output parent directory does not exist: {path.parent}")


def atomic_publish(path: Path, data: bytes) -> None:
    """Publish a complete file atomically without replacing any existing name.

    A same-directory hard link provides atomic create-if-absent on NTFS/POSIX.
    Filesystems without hard-link support fail closed; no unsafe fallback.
    """
    fd, temporary = tempfile.mkstemp(prefix=".journey-gtfs-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="GTFS directory or root-level ZIP; read-only")
    parser.add_argument("--date", required=True, help="Snapshot local date, YYYY-MM-DD; 48-hour horizon")
    parser.add_argument("--feed-id", required=True, help="Stable namespace: 1..32 ASCII letters/digits/._-")
    parser.add_argument("--source", required=True, help="Publisher/source description or public provenance URL")
    parser.add_argument("--synthetic", action="store_true", help="Mark fictional/test data is_demo=true")
    parser.add_argument("--walking-config", type=Path, help="Explicit walking links/margins using original GTFS stop IDs")
    parser.add_argument("--output", required=True, type=Path, help="New timetable JSON path, outside the input directory")
    parser.add_argument("--report", required=True, type=Path, help="New provenance/loss report JSON path")
    args = parser.parse_args(argv)
    try:
        validate_output_paths(args.input, args.walking_config, args.output, args.report)
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", args.date):
            raise ValueError("--date must use YYYY-MM-DD")
        day = date.fromisoformat(args.date)
        timetable, report = import_gtfs(args.input, service_date=day, feed_id=args.feed_id, source=args.source,
                                       synthetic=args.synthetic, walking_config=args.walking_config)
        # The report is published first; the validated timetable is the final
        # publication marker. Two filenames are NOT a transactional pair.
        atomic_publish(args.report, json_bytes(report))
        if timetable is not None:
            atomic_publish(args.output, json_bytes(timetable.model_dump(mode="json")))
        print(json.dumps({"status": report["status"], "summary": report.get("summary"),
                          "report": str(args.report), "timetable": str(args.output) if timetable else None}, ensure_ascii=False))
        return 0 if timetable is not None else 2
    except (OSError, ValueError) as exc:
        print(f"GTFS import failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
