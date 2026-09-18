from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from app.config import INTERCITY_CITY_NAME, Settings, get_settings, guess_city_from_routeid, to_intercity_routeid
from app.db import (
    get_connection,
    init_db,
    load_route_static,
    load_tdx_fetch_state,
    route_exists,
    save_tdx_fetch_state,
)
from app.logging_utils import get_logger, setup_logging, shutdown_logging
from app.ntpc_opendata import NtpcOpenDataClient
from app.route_aliases import get_route_alias_index
from app.tdx_auth import TDXTokenManager
from app.tdx_client import TDXClient, TDXJSONResponse


STOP_STATUS_MESSAGES = {
    1: "\u5c1a\u672a\u767c\u8eca",
    2: "\u4ea4\u7ba1\u4e0d\u505c\u9760",
    3: "末班駛離",
    4: "\u4eca\u65e5\u672a\u71df\u904b",
}

ARRIVING_VEHICLE_STOP_STATUS = 1
REALTIME_FETCH_STATE_RETENTION_SECONDS = 86400
TDX_SOURCE = "tdx"
BACKFILL_BUSES_SOURCE = "backfill_buses"
NTPC_OPENDATA_SOURCE = "ntpc_opendata"
ETA_SOURCES = {TDX_SOURCE, BACKFILL_BUSES_SOURCE, NTPC_OPENDATA_SOURCE}
BACKFILL_MAX_STOP_DISTANCE_METERS = 500.0
BACKFILL_MAX_ANCHOR_STOP_DISTANCE_METERS = 150.0
BACKFILL_MAX_DISAPPEARANCE_SECONDS = 90
BACKFILL_MAX_BUS_AGE_SECONDS = 90
DISTANCE_FALLBACK_ROUTE_FACTOR = 1.15
DISTANCE_FALLBACK_SPEED_METERS_PER_SECOND = 5.5
DISTANCE_FALLBACK_STOP_DWELL_SECONDS = 20
DISTANCE_FALLBACK_MIN_SEGMENT_SECONDS = 30
DISTANCE_FALLBACK_MAX_SEGMENT_SECONDS = 240
LOGGER = get_logger("sync_realtime")


@dataclass
class PlateObservation:
    plate: str
    pathid: int
    stopid: str
    eta: int | None
    is_arriving: bool


@dataclass
class LastNativePlateState:
    plate: str
    pathid: int
    stopid: str
    stop_seq: int | None
    last_seen_ts: int
    is_terminal: bool


class RouteNotFoundError(KeyError):
    """Raised when a route is missing from the local database."""


@dataclass
class CacheEntry:
    snapshot: dict[str, Any]
    expires_at: float


@dataclass
class BusesCacheEntry:
    buses: list[dict[str, Any]]
    expires_at: float


def _tdx_routeid_to_local(city: str, routeid: Any, *, settings: Settings | None = None) -> str | None:
    """Map a SubRouteUID from a TDX response onto the local canonical routeid.

    A merged route is queried by every SubRouteUID it absorbed, so responses
    arrive tagged with the original per-direction ids and have to be collapsed
    back before they can be bucketed. ``_build_snapshot`` then files each item
    by its ``Direction``, which the merge preserves as the pathid.
    """
    if routeid is None:
        return None
    normalized = str(routeid).strip()
    if not normalized:
        return None
    if city == INTERCITY_CITY_NAME:
        normalized = to_intercity_routeid(normalized)
    if settings is None:
        return normalized
    return get_route_alias_index(settings).canonical(normalized)


def _tdx_item_to_local(
    city: str,
    item: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> str | None:
    """Resolve a TDX realtime item onto the local canonical routeid.

    Items normally carry SubRouteUID and resolve through the alias table. 雙北
    feeds stopped populating SubRouteUID — their items are tagged with only
    RouteUID + Direction — so those fall back to the persisted/derived RouteUID
    mapping. A RouteUID shared by several routes (區間/lettered variants)
    resolves to None and the item is dropped rather than guessed at.
    """
    subroute_uid = item.get("SubRouteUID")
    if subroute_uid is not None and str(subroute_uid).strip():
        return _tdx_routeid_to_local(city, subroute_uid, settings=settings)

    route_uid = str(item.get("RouteUID") or "").strip()
    if not route_uid:
        return None
    if settings is not None:
        try:
            direction: int | None = int(item.get("Direction"))
        except (TypeError, ValueError):
            direction = None
        local = get_route_alias_index(settings).routeid_for_route_uid(
            route_uid, direction
        )
        if local is not None:
            return local
    # Legacy shape: treat the RouteUID as a routeid, which is exact for
    # identity-style ids and mirrors the old SubRouteUID-or-RouteUID fallback.
    return _tdx_routeid_to_local(city, route_uid, settings=settings)


def _tdx_item_to_local_strict(
    city: str,
    item: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> tuple[str | None, str | None, int | None]:
    """Resolve a realtime item without ever guessing a routeid.

    Returns ``(routeid, route_uid, direction)``. Unlike ``_tdx_item_to_local``
    this never falls back to treating a RouteUID as a routeid: that fallback is
    only safe because every existing caller then drops ids missing from its
    tracked/static route set, and a city-wide feed has no such set. An
    unresolvable item keeps its RouteUID so the caller can still show the bus
    and describe it through ``RouteAliasIndex.family_routeids``.

    ``route_uid`` is None only when the item carries no usable identity at all,
    which means the caller should drop it.
    """
    try:
        direction: int | None = int(item.get("Direction"))
    except (TypeError, ValueError):
        direction = None

    index = get_route_alias_index(settings) if settings is not None else None

    subroute_uid = item.get("SubRouteUID")
    if subroute_uid is not None and str(subroute_uid).strip():
        routeid = _tdx_routeid_to_local(city, subroute_uid, settings=settings)
        route_uid = str(item.get("RouteUID") or "").strip()
        if not route_uid and routeid is not None and index is not None:
            route_uid = index.route_uid_for(routeid) or ""
        return routeid, route_uid or routeid, direction

    route_uid = str(item.get("RouteUID") or "").strip()
    if not route_uid:
        return None, None, direction
    if index is None:
        return None, route_uid, direction
    return index.routeid_for_route_uid(route_uid, direction), route_uid, direction


def _to_unix_seconds(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _to_hhmm(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return dt.strftime("%H:%M")


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _eta_source(value: Any) -> str:
    source = str(value or "").strip()
    if source in ETA_SOURCES:
        return source
    return TDX_SOURCE


def _adjusted_eta(
    data: dict[str, Any],
    *,
    now_ts: int,
    fallback_update_time: str | None = None,
) -> int | None:
    estimate_time = _to_int_or_none(data.get("EstimateTime"))
    if estimate_time is None:
        return None

    update_time = (
        data.get("SrcUpdateTime")
        or data.get("UpdateTime")
        or fallback_update_time
    )
    update_ts = _to_unix_seconds(update_time)
    if update_ts is None:
        return estimate_time

    eta = estimate_time - (now_ts - update_ts)
    return max(-1, eta)


def _item_updated_at(item: dict[str, Any]) -> int | None:
    return (
        _to_unix_seconds(item.get("UpdateTime"))
        or _to_unix_seconds(item.get("DataTime"))
        or _to_unix_seconds(item.get("SrcRecTime"))
        or _to_unix_seconds(item.get("SrcTransTime"))
        or _to_unix_seconds(item.get("SrcUpdateTime"))
        or _to_unix_seconds(item.get("TransTime"))
    )


def _build_message(item: dict[str, Any], *, now_ts: int) -> str:
    if _adjusted_eta(item, now_ts=now_ts) is not None:
        return ""

    stop_status = _to_int_or_none(item.get("StopStatus"))
    if stop_status == 1:
        scheduled_time = (item.get("ScheduledTime") or "").strip()
        if scheduled_time:
            return scheduled_time
        next_bus_time = _to_hhmm(item.get("NextBusTime"))
        if next_bus_time:
            return next_bus_time
        return STOP_STATUS_MESSAGES[1]

    if stop_status in {2, 3, 4}:
        return STOP_STATUS_MESSAGES[stop_status]

    if item.get("IsLastBus"):
        return STOP_STATUS_MESSAGES[3]

    scheduled_time = (item.get("ScheduledTime") or "").strip()
    if scheduled_time:
        return scheduled_time

    return ""


def _normalize_plate(value: Any) -> str | None:
    if value is None:
        return None
    plate = str(value).strip()
    if not plate or plate == "-1":
        return None
    return plate


def _pathid_for_ntpc_eta_row(
    static_route: dict[str, Any],
    *,
    stopid: str,
    goback: Any,
) -> int | None:
    paths = static_route.get("paths") or {}
    matching_pathids = [
        pathid
        for pathid, path_meta in paths.items()
        if stopid in (path_meta.get("stop_index") or {})
    ]
    preferred_pathid = _to_int_or_none(goback)
    if preferred_pathid in matching_pathids:
        return preferred_pathid
    if len(matching_pathids) == 1:
        return matching_pathids[0]
    return None


def _build_ntpc_eta_items(
    routeid: str,
    static_route: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        stopid = str(row.get("stopid") or "").strip()
        if not stopid:
            continue
        pathid = _pathid_for_ntpc_eta_row(
            static_route,
            stopid=stopid,
            goback=row.get("goback"),
        )
        if pathid is None:
            continue

        estimate_time = _to_int_or_none(row.get("estimatetime"))
        item: dict[str, Any] = {
            "RouteUID": routeid,
            "SubRouteUID": routeid,
            "Direction": pathid,
            "StopID": stopid,
            "_source": NTPC_OPENDATA_SOURCE,
        }
        if estimate_time is None:
            continue
        if estimate_time < 0:
            continue
        item["EstimateTime"] = estimate_time
        if estimate_time == 0:
            item["VehicleStopStatus"] = ARRIVING_VEHICLE_STOP_STATUS
        items.append(item)
    return items


def _append_stop_eta(
    stop_bucket: dict[str, Any],
    *,
    plate: str | None,
    eta: int | None,
    is_arriving: bool,
    source: str = TDX_SOURCE,
    estimated: bool = False,
) -> None:
    if eta is None:
        return
    stop_bucket.setdefault("etas", []).append(
        {
            "plate": plate,
            "eta": eta,
            "is_arriving": is_arriving,
            "source": source,
            "estimated": estimated,
        }
    )


def _finalize_stop_eta_list(stop_bucket: dict[str, Any]) -> None:
    raw_etas = stop_bucket.get("etas") or []
    if not isinstance(raw_etas, list) or not raw_etas:
        stop_bucket["etas"] = []
        return

    deduped: dict[str, dict[str, Any]] = {}
    for entry in raw_etas:
        if not isinstance(entry, dict):
            continue

        plate = _normalize_plate(entry.get("plate"))
        eta = _to_int_or_none(entry.get("eta"))
        is_arriving = bool(entry.get("is_arriving"))
        source = _eta_source(entry.get("source"))
        estimated = bool(entry.get("estimated"))
        if eta is None:
            continue

        key = plate or f"anon:{eta}"
        candidate = {
            "plate": plate,
            "eta": eta,
            "is_arriving": is_arriving,
            "source": source,
            "estimated": estimated,
        }
        current = deduped.get(key)
        if current is None:
            deduped[key] = candidate
            continue

        current_score = (
            0 if current.get("is_arriving") else 1,
            current.get("eta") if current.get("eta") is not None else 10**9,
            0 if current.get("source") == TDX_SOURCE else 1,
        )
        candidate_score = (
            0 if candidate.get("is_arriving") else 1,
            candidate.get("eta") if candidate.get("eta") is not None else 10**9,
            0 if candidate.get("source") == TDX_SOURCE else 1,
        )
        if candidate_score < current_score:
            deduped[key] = candidate

    stop_bucket["etas"] = sorted(
        deduped.values(),
        key=lambda entry: (
            0 if entry.get("is_arriving") else 1,
            entry.get("eta") if entry.get("eta") is not None else 10**9,
            0 if entry.get("source") == TDX_SOURCE else 1,
            entry.get("plate") or "",
        ),
    )

    best_eta = next(
        (
            entry.get("eta")
            for entry in stop_bucket["etas"]
            if entry.get("eta") is not None
        ),
        None,
    )
    current_eta = _to_int_or_none(stop_bucket.get("eta"))
    if best_eta is not None and (current_eta is None or best_eta < current_eta):
        stop_bucket["eta"] = best_eta
    if _to_int_or_none(stop_bucket.get("eta")) is not None:
        stop_bucket["message"] = ""
    updated_at = _to_int_or_none(stop_bucket.get("updated_at"))
    if updated_at is not None:
        stop_bucket["updated_at"] = updated_at
    else:
        stop_bucket.pop("updated_at", None)


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = lat2_rad - lat1_rad
    delta_lon = math.radians(lon2 - lon1)
    mean_lat = (lat1_rad + lat2_rad) / 2
    earth_radius = 6371000.0
    x = delta_lon * math.cos(mean_lat)
    y = delta_lat
    return math.hypot(x, y) * earth_radius


def _create_stop_bucket(stopid: str) -> dict[str, Any]:
    return {
        "stopid": stopid,
        "eta": None,
        "message": "",
        "updated_at": None,
        "buses": [],
        "etas": [],
    }


def _set_stop_bucket_updated_at(stop_bucket: dict[str, Any], timestamp: int | None) -> None:
    if timestamp is None:
        return
    current = _to_int_or_none(stop_bucket.get("updated_at"))
    if current is None or timestamp > current:
        stop_bucket["updated_at"] = timestamp


def _max_path_stop_seq(path_meta: dict[str, Any]) -> int | None:
    max_seq: int | None = None
    for stop in path_meta.get("stops") or []:
        if not isinstance(stop, dict):
            continue
        seq = _to_int_or_none(stop.get("seq"))
        if seq is None:
            continue
        if max_seq is None or seq > max_seq:
            max_seq = seq
    return max_seq


def _stop_bucket_has_live_data(stop_bucket: dict[str, Any]) -> bool:
    return (
        _to_int_or_none(stop_bucket.get("eta")) is not None
        or bool(str(stop_bucket.get("message") or "").strip())
        or bool(stop_bucket.get("buses"))
        or bool(stop_bucket.get("etas"))
    )


def _prune_expired_backfill(snapshot: dict[str, Any], *, now_ts: int) -> None:
    updated_at = _to_int_or_none(snapshot.get("updated_at"))
    if updated_at is None or now_ts - updated_at <= BACKFILL_MAX_DISAPPEARANCE_SECONDS:
        return

    for path in snapshot.get("paths") or []:
        if not isinstance(path, dict):
            continue
        next_stops: list[dict[str, Any]] = []
        for stop_bucket in path.get("stops") or []:
            if not isinstance(stop_bucket, dict):
                continue
            original_buses = stop_bucket.get("buses") or []
            original_etas = stop_bucket.get("etas") or []
            filtered_buses = [
                bus
                for bus in original_buses
                if isinstance(bus, dict) and bus.get("source") != BACKFILL_BUSES_SOURCE
            ]
            filtered_etas = [
                eta
                for eta in original_etas
                if isinstance(eta, dict) and eta.get("source") != BACKFILL_BUSES_SOURCE
            ]
            if (
                len(filtered_buses) != len(original_buses)
                or len(filtered_etas) != len(original_etas)
            ):
                stop_bucket["buses"] = filtered_buses
                stop_bucket["etas"] = filtered_etas
                stop_bucket["eta"] = None
                _finalize_stop_eta_list(stop_bucket)
            if _stop_bucket_has_live_data(stop_bucket):
                next_stops.append(stop_bucket)
        path["stops"] = next_stops


def _find_nearest_stop(
    path_meta: dict[str, Any],
    *,
    lat: float,
    lon: float,
) -> tuple[int, dict[str, Any], float] | None:
    raw_stops = path_meta.get("stops") or []
    if not raw_stops:
        return None

    stops = sorted(
        (stop for stop in raw_stops if isinstance(stop, dict)),
        key=lambda stop: (
            _to_int_or_none(stop.get("seq")) or 10**9,
            stop.get("stopid") or "",
        ),
    )
    nearest_index = -1
    nearest_stop: dict[str, Any] | None = None
    nearest_distance = float("inf")
    for index, stop in enumerate(stops):
        stop_lat = _to_float_or_none(stop.get("lat"))
        stop_lon = _to_float_or_none(stop.get("lon"))
        if stop_lat is None or stop_lon is None:
            continue
        distance = _distance_meters(lat, lon, stop_lat, stop_lon)
        if distance < nearest_distance:
            nearest_distance = distance
            nearest_stop = stop
            nearest_index = index

    if nearest_stop is None or nearest_index == -1:
        return None
    return nearest_index, stops[nearest_index], nearest_distance


def _load_travel_time_segments_by_path(
    db_path: str | Path,
    routeid: str,
) -> dict[int, dict[tuple[int, int], int]]:
    with get_connection(db_path) as connection:
        try:
            rows = connection.execute(
                """
                SELECT direction, from_seq, to_seq, avg_seconds, source
                FROM stop_travel_times
                WHERE routeid = ?
                """,
                (routeid,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "stop_travel_times" not in str(exc):
                raise
            rows = []

    segments_by_path: dict[int, dict[tuple[int, int], int]] = {}
    segment_sources: dict[int, dict[tuple[int, int], str]] = {}
    for row in rows:
        pathid = _to_int_or_none(row["direction"])
        from_seq = _to_int_or_none(row["from_seq"])
        to_seq = _to_int_or_none(row["to_seq"])
        avg_seconds = _to_float_or_none(row["avg_seconds"])
        source = row["source"] if row["source"] in {"eta", "timetable"} else "timetable"
        if pathid is None or from_seq is None or to_seq is None or avg_seconds is None:
            continue

        path_segments = segments_by_path.setdefault(pathid, {})
        path_segment_sources = segment_sources.setdefault(pathid, {})
        key = (from_seq, to_seq)
        if key not in path_segments or (
            path_segment_sources.get(key) != "eta" and source == "eta"
        ):
            path_segments[key] = max(0, int(round(avg_seconds)))
            path_segment_sources[key] = source

    return segments_by_path


def _estimate_distance_fallback_segment_seconds(
    from_stop: dict[str, Any],
    to_stop: dict[str, Any],
) -> int | None:
    from_lat = _to_float_or_none(from_stop.get("lat"))
    from_lon = _to_float_or_none(from_stop.get("lon"))
    to_lat = _to_float_or_none(to_stop.get("lat"))
    to_lon = _to_float_or_none(to_stop.get("lon"))
    if None in {from_lat, from_lon, to_lat, to_lon}:
        return None
    if not all(math.isfinite(value) for value in (from_lat, from_lon, to_lat, to_lon)):
        return None

    distance = _distance_meters(from_lat, from_lon, to_lat, to_lon)
    if distance <= 0:
        return DISTANCE_FALLBACK_MIN_SEGMENT_SECONDS

    adjusted_distance = distance * DISTANCE_FALLBACK_ROUTE_FACTOR
    seconds = int(
        round(
            adjusted_distance / DISTANCE_FALLBACK_SPEED_METERS_PER_SECOND
            + DISTANCE_FALLBACK_STOP_DWELL_SECONDS
        )
    )
    return max(
        DISTANCE_FALLBACK_MIN_SEGMENT_SECONDS,
        min(DISTANCE_FALLBACK_MAX_SEGMENT_SECONDS, seconds),
    )


def _build_distance_fallback_segments(
    path_meta: dict[str, Any],
) -> dict[tuple[int, int], int]:
    path_stops = sorted(
        (stop for stop in path_meta.get("stops") or [] if isinstance(stop, dict)),
        key=lambda stop: (_to_int_or_none(stop.get("seq")) or 10**9, stop.get("stopid") or ""),
    )
    if len(path_stops) < 2:
        return {}

    segments: dict[tuple[int, int], int] = {}
    previous_stop: dict[str, Any] | None = None
    previous_seq: int | None = None
    for stop in path_stops:
        current_seq = _to_int_or_none(stop.get("seq"))
        if current_seq is None:
            continue
        if previous_stop is not None and previous_seq is not None and current_seq > previous_seq:
            segment_seconds = _estimate_distance_fallback_segment_seconds(previous_stop, stop)
            if segment_seconds is not None:
                segments[(previous_seq, current_seq)] = segment_seconds
        previous_stop = stop
        previous_seq = current_seq

    return segments


def _collect_plate_observations(
    item: dict[str, Any],
    *,
    pathid: int,
    stopid: str,
    now_ts: int,
) -> list[PlateObservation]:
    observations: list[PlateObservation] = []

    top_plate = _normalize_plate(item.get("PlateNumb"))
    top_eta = _adjusted_eta(item, now_ts=now_ts)
    top_is_arriving = _to_int_or_none(item.get("VehicleStopStatus")) == ARRIVING_VEHICLE_STOP_STATUS
    if top_plate is not None and top_eta is not None:
        observations.append(
            PlateObservation(
                plate=top_plate,
                pathid=pathid,
                stopid=stopid,
                eta=top_eta,
                is_arriving=top_is_arriving,
            )
        )

    for estimate in item.get("Estimates") or []:
        estimate_plate = _normalize_plate(estimate.get("PlateNumb"))
        estimate_eta = _adjusted_eta(
            estimate,
            now_ts=now_ts,
            fallback_update_time=item.get("SrcUpdateTime") or item.get("UpdateTime"),
        )
        if estimate_plate is None or estimate_eta is None:
            continue
        estimate_is_arriving = (
            _to_int_or_none(estimate.get("VehicleStopStatus")) == ARRIVING_VEHICLE_STOP_STATUS
        )
        observations.append(
            PlateObservation(
                plate=estimate_plate,
                pathid=pathid,
                stopid=stopid,
                eta=estimate_eta,
                is_arriving=estimate_is_arriving,
            )
        )

    return observations


def _extract_and_store_eta_travel_times(
    snapshot: dict[str, Any],
    static_route: dict[str, Any],
    db_path: str | Path,
) -> None:
    """Derives inter-stop travel times from live ETA observations.

    When a bus is physically on the road, TDX provides an ETA (seconds
    until arrival) for every stop along its path.  For a single bus
    (identified by plate number), the difference between two consecutive
    stops' ETAs gives the travel time between them:

        travel_time(stop_A → stop_B) = ETA_B − ETA_A

    These observations are accumulated into the *stop_travel_times* table
    alongside the timetable-derived data.  ETA-sourced entries use
    ``source = 'eta'`` and are preferred over timetable-derived values
    because they reflect real-world conditions (traffic, dwell times, …).

    The function is called after each successful realtime refresh so that
    the travel-time database improves continuously.
    """
    paths_meta = static_route.get("paths") or {}

    for path_data in snapshot.get("paths") or []:
        pathid = path_data.get("pathid")
        if pathid is None:
            continue

        path_meta = paths_meta.get(pathid)
        if not path_meta:
            continue
        stop_index = path_meta.get("stop_index") or {}

        # Collect per-plate ETA vectors: plate → [(seq, eta_seconds)]
        plate_etas: dict[str, list[tuple[int, int]]] = {}
        for stop_data in path_data.get("stops") or []:
            stopid = stop_data.get("stopid")
            if not stopid:
                continue
            seq_info = stop_index.get(stopid)
            if not seq_info:
                continue
            seq = seq_info.get("seq")
            if seq is None:
                continue

            for eta_entry in stop_data.get("etas") or []:
                if not isinstance(eta_entry, dict):
                    continue
                if eta_entry.get("source") == BACKFILL_BUSES_SOURCE:
                    continue
                plate = _normalize_plate(eta_entry.get("plate"))
                eta_val = _to_int_or_none(eta_entry.get("eta"))
                if plate is None or eta_val is None:
                    continue
                plate_etas.setdefault(plate, []).append((int(seq), eta_val))

        # For each plate, sort by seq and compute inter-stop deltas.
        observations: dict[tuple[str, int, int, int], list[float]] = {}
        for plate, seq_etas in plate_etas.items():
            seq_etas.sort(key=lambda x: x[0])
            for i in range(len(seq_etas) - 1):
                from_seq, from_eta = seq_etas[i]
                to_seq, to_eta = seq_etas[i + 1]
                delta = to_eta - from_eta
                if delta <= 0 or delta > 180 * 60:
                    # Skip impossible/unreasonable deltas (> 3h).
                    continue
                routeid = snapshot.get("routeid", "")
                key = (routeid, pathid, from_seq, to_seq)
                observations.setdefault(key, []).append(float(delta))

        if not observations:
            continue

        # Persist into stop_travel_times with source='eta'.
        routeid = snapshot.get("routeid", "")
        with get_connection(db_path) as connection:
            with connection:
                for (rid, direction, from_seq, to_seq), secs_list in observations.items():
                    avg_seconds = sum(secs_list) / len(secs_list)
                    sample_count = len(secs_list)
                    # Check if an ETA-sourced row already exists.
                    existing = connection.execute(
                        """
                        SELECT avg_seconds, sample_count
                        FROM stop_travel_times
                        WHERE routeid = ? AND direction = ?
                          AND from_seq = ? AND to_seq = ?
                          AND source = 'eta'
                        """,
                        (rid, direction, from_seq, to_seq),
                    ).fetchone()
                    if existing is not None:
                        # Merge with existing ETA observation using weighted average.
                        old_avg = existing["avg_seconds"]
                        old_count = existing["sample_count"]
                        total_count = old_count + sample_count
                        merged_avg = (old_avg * old_count + avg_seconds * sample_count) / total_count
                        connection.execute(
                            """
                            UPDATE stop_travel_times
                            SET avg_seconds = ?, sample_count = ?
                            WHERE routeid = ? AND direction = ?
                              AND from_seq = ? AND to_seq = ?
                              AND source = 'eta'
                            """,
                            (merged_avg, total_count, rid, direction, from_seq, to_seq),
                        )
                    else:
                        connection.execute(
                            """
                            INSERT OR REPLACE INTO stop_travel_times
                                (routeid, direction, from_seq, to_seq,
                                 avg_seconds, sample_count, source)
                            VALUES (?, ?, ?, ?, ?, ?, 'eta')
                            """,
                            (rid, direction, from_seq, to_seq, avg_seconds, sample_count),
                        )


def _batched_resource_key(kind: str, city: str, routeids: list[str]) -> str:
    joined = "\n".join(routeids).encode("utf-8")
    digest = hashlib.sha1(joined).hexdigest()[:16]
    return f"{kind}:{city}:{len(routeids)}:{digest}"


def _persist_fetch_state(
    connection,
    resource_key: str,
    response: TDXJSONResponse,
    checked_at: int,
    previous_state: dict | None,
) -> None:
    previous_last_modified = None if previous_state is None else previous_state.get("last_modified")
    previous_updated_at = None if previous_state is None else previous_state.get("last_updated_at")
    effective_last_modified = response.last_modified or previous_last_modified

    save_tdx_fetch_state(
        connection,
        resource_key,
        last_modified=effective_last_modified,
        last_status=response.status_code,
        last_checked_at=checked_at,
        last_updated_at=checked_at if not response.not_modified else previous_updated_at,
    )


def _prune_old_realtime_fetch_state(connection, now: int) -> None:
    connection.execute(
        """
        DELETE FROM tdx_fetch_state
        WHERE (
            resource_key LIKE 'realtime_eta:%'
            OR resource_key LIKE 'realtime_buses:%'
        )
          AND last_checked_at < ?
        """,
        (now - REALTIME_FETCH_STATE_RETENTION_SECONDS,),
    )


def _build_buses_payload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buses_by_plate: dict[str, dict[str, Any]] = {}

    for item in items:
        if _to_int_or_none(item.get("DutyStatus")) == 2:
            continue

        plate = _normalize_plate(item.get("PlateNumb"))
        if plate is None:
            continue

        position = item.get("BusPosition") or {}
        lat_raw = position.get("PositionLat")
        lon_raw = position.get("PositionLon")
        if lat_raw is None or lon_raw is None:
            continue

        try:
            lat = float(lat_raw)
            lon = float(lon_raw)
        except (TypeError, ValueError):
            continue

        bus = {
            "id": plate,
            "direction": item.get("Direction"),
            "lat": lat,
            "lon": lon,
            "speed": item.get("Speed"),
            "azimuth": item.get("Azimuth"),
            "status": item.get("BusStatus"),
            "time": _to_unix_seconds(item.get("GPSTime")),
        }

        existing = buses_by_plate.get(plate)
        if existing is None:
            buses_by_plate[plate] = bus
            continue

        existing_time = existing.get("time")
        next_time = bus.get("time")
        if next_time is not None and (existing_time is None or next_time > existing_time):
            buses_by_plate[plate] = bus

    return list(buses_by_plate.values())


def _backfill_snapshot_from_buses(
    *,
    routeid: str,
    db_path: str | Path,
    paths: dict[int, dict[str, Any]],
    grouped: dict[int, dict[str, dict[str, Any]]],
    native_plates_by_path: dict[int, set[str]],
    disappeared_native_plates_by_path: dict[int, dict[str, LastNativePlateState]],
    buses: list[dict[str, Any]],
    now_ts: int,
) -> None:
    if not buses:
        return

    travel_time_segments = _load_travel_time_segments_by_path(db_path, routeid)
    fallback_segments_by_path: dict[int, dict[tuple[int, int], int]] = {}
    backfilled_plates_by_path: dict[int, set[str]] = defaultdict(set)

    for bus in buses:
        plate = _normalize_plate(bus.get("id"))
        pathid = _to_int_or_none(bus.get("direction"))
        lat = _to_float_or_none(bus.get("lat"))
        lon = _to_float_or_none(bus.get("lon"))
        bus_time = _to_int_or_none(bus.get("time"))
        if plate is None or pathid is None or lat is None or lon is None:
            continue
        path_native_plates = native_plates_by_path.get(pathid, set())
        path_backfilled_plates = backfilled_plates_by_path[pathid]
        if plate in path_native_plates or plate in path_backfilled_plates:
            continue
        disappeared = disappeared_native_plates_by_path.get(pathid, {}).get(plate)
        if disappeared is None:
            continue
        if bus_time is None or now_ts - bus_time > BACKFILL_MAX_BUS_AGE_SECONDS:
            continue

        path_meta = paths.get(pathid)
        if not path_meta:
            continue

        nearest = _find_nearest_stop(path_meta, lat=lat, lon=lon)
        if nearest is None:
            continue

        anchor_index, anchor_stop, anchor_distance = nearest
        if anchor_distance > BACKFILL_MAX_STOP_DISTANCE_METERS:
            continue
        if disappeared is not None and disappeared.is_terminal:
            continue
        if (
            disappeared is not None
            and now_ts - disappeared.last_seen_ts > BACKFILL_MAX_DISAPPEARANCE_SECONDS
        ):
            continue
        if disappeared is not None and disappeared.stop_seq is not None:
            anchor_seq = _to_int_or_none(anchor_stop.get("seq"))
            if anchor_seq is None or anchor_seq < disappeared.stop_seq:
                continue

        stopid = anchor_stop.get("stopid")
        if not stopid:
            continue

        path_bucket = grouped.setdefault(pathid, {})
        stop_bucket = path_bucket.setdefault(stopid, _create_stop_bucket(stopid))
        _set_stop_bucket_updated_at(stop_bucket, bus_time)
        if all(_normalize_plate(existing.get("id")) != plate for existing in stop_bucket["buses"]):
            stop_bucket["buses"].append(
                {"id": plate, "type": "normal", "source": BACKFILL_BUSES_SOURCE}
            )

        path_backfilled_plates.add(plate)

        stop_seq = _to_int_or_none(anchor_stop.get("seq"))
        if stop_seq is None:
            continue

        anchor_eta = 0 if anchor_distance <= BACKFILL_MAX_ANCHOR_STOP_DISTANCE_METERS else None
        if anchor_eta is not None:
            _append_stop_eta(
                stop_bucket,
                plate=plate,
                eta=anchor_eta,
                is_arriving=anchor_eta <= 0,
                source=BACKFILL_BUSES_SOURCE,
                estimated=True,
            )

        path_stops = sorted(
            (stop for stop in path_meta.get("stops") or [] if isinstance(stop, dict)),
            key=lambda stop: (_to_int_or_none(stop.get("seq")) or 10**9, stop.get("stopid") or ""),
        )
        if not path_stops:
            continue

        fallback_segments = fallback_segments_by_path.get(pathid)
        if fallback_segments is None:
            fallback_segments = _build_distance_fallback_segments(path_meta)
            fallback_segments_by_path[pathid] = fallback_segments
        segments = dict(fallback_segments)
        segments.update(travel_time_segments.get(pathid, {}))
        running_eta = anchor_eta
        previous_seq = stop_seq
        started = False
        for stop in path_stops:
            current_seq = _to_int_or_none(stop.get("seq"))
            current_stopid = stop.get("stopid")
            if current_seq is None or not current_stopid:
                continue
            if current_seq < stop_seq:
                continue
            if current_seq == stop_seq:
                started = True
                continue
            if not started:
                continue

            segment_seconds = segments.get((previous_seq, current_seq))
            if segment_seconds is None:
                break

            running_eta = segment_seconds if running_eta is None else running_eta + segment_seconds
            current_bucket = path_bucket.setdefault(current_stopid, _create_stop_bucket(current_stopid))
            _set_stop_bucket_updated_at(current_bucket, bus_time)
            _append_stop_eta(
                current_bucket,
                plate=plate,
                eta=running_eta,
                is_arriving=False,
                source=BACKFILL_BUSES_SOURCE,
                estimated=True,
            )
            previous_seq = current_seq


class RealtimeService:
    def __init__(
        self,
        settings: Settings,
        client: TDXClient,
        route_buses_service: "RouteBusesService | None" = None,
        ntpc_opendata_client: NtpcOpenDataClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.route_buses_service = route_buses_service
        self.ntpc_opendata_client = ntpc_opendata_client
        self._cache: dict[str, CacheEntry] = {}
        self._cache_lock = threading.Lock()
        self._last_native_plate_states: dict[str, dict[tuple[int, str], LastNativePlateState]] = {}
        self._last_native_plate_states_lock = threading.Lock()
        self._tracked_routes: dict[str, dict[str, float]] = {}
        self._tracked_routes_lock = threading.Lock()
        self._city_refresh_locks: dict[str, threading.Lock] = {}
        self._city_refresh_locks_guard = threading.Lock()

    def get_snapshot(self, routeid: str, *, force_refresh: bool = False) -> dict[str, Any]:
        # A routeid absorbed by a direction merge still has to resolve, or every
        # favorite and widget pointing at the return direction goes dark.
        routeid = get_route_alias_index(self.settings).canonical(routeid)
        static_route = self._load_static_route(routeid)
        if static_route is None:
            raise RouteNotFoundError(routeid)

        city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if city is None:
            return self._get_single_route_snapshot(routeid, static_route, force_refresh=force_refresh)

        self._track_route(city, routeid)

        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        city_lock = self._get_city_refresh_lock(city)
        with city_lock:
            if not force_refresh:
                cached = self._get_cached(routeid)
                if cached is not None:
                    return cached

            try:
                self._refresh_city_cache(city, routeid, static_route, force_refresh=force_refresh)
            except Exception:
                stale = self._get_cached(routeid, allow_expired=True)
                if stale is not None:
                    LOGGER.warning(
                        "using stale realtime cache routeid=%s city=%s after refresh failure",
                        routeid,
                        city,
                    )
                    self._set_cached(routeid, stale)
                    return stale
                raise

            cached = self._get_cached(routeid, allow_expired=True)
            if cached is not None:
                self._set_cached(routeid, cached)
                return cached

        return self._build_snapshot(routeid, static_route, [])

    def get_batch_snapshots(self, routeids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch realtime snapshots for multiple route IDs at once.

        Routes are grouped by city so each city generates a single TDX batch
        request, drastically reducing the number of HTTP calls compared to
        requesting each route individually.  Results are served from the
        per-route cache when fresh; only routes whose cache entries are stale
        or missing trigger a TDX refresh.

        Returns a mapping of routeid -> snapshot for every route that was
        successfully resolved.  Unknown route IDs are silently omitted.

        Stale routeids that a merge absorbed resolve to the surviving route, and
        the snapshot is echoed back under both keys so widgets that still hold
        the old id keep rendering.
        """
        alias_index = get_route_alias_index(self.settings)
        aliases_by_canonical: dict[str, list[str]] = {}
        for routeid in routeids:
            canonical = alias_index.canonical(routeid)
            if canonical != routeid:
                aliases_by_canonical.setdefault(canonical, []).append(routeid)

        deduped_routeids = sorted({alias_index.canonical(routeid) for routeid in routeids})
        if not deduped_routeids:
            return {}

        # Pre-populate from cache.
        results: dict[str, dict[str, Any]] = {}
        routes_needing_refresh: list[str] = []
        for routeid in deduped_routeids:
            cached = self._get_cached(routeid)
            if cached is not None:
                results[routeid] = cached
            else:
                routes_needing_refresh.append(routeid)

        # Group routes that still need a refresh by city.
        routes_by_city: dict[str, list[str]] = {}
        for routeid in routes_needing_refresh:
            city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
            if city is not None:
                routes_by_city.setdefault(city, []).append(routeid)

        # Refresh each city group.
        for city, city_routeids in routes_by_city.items():
            # Ensure all routes are tracked so _refresh_city_cache_for_routes
            # includes them in the batch.
            for routeid in city_routeids:
                self._track_route(city, routeid)

            city_lock = self._get_city_refresh_lock(city)
            with city_lock:
                # Another single/batch request may have refreshed these routes
                # while this caller waited for the city lock.
                missing = [routeid for routeid in city_routeids if self._get_cached(routeid) is None]
                try:
                    if missing:
                        self._refresh_city_cache_for_routes(city, missing)
                except Exception:
                    LOGGER.warning(
                        "batch realtime refresh failed for city=%s routes=%s",
                        city,
                        len(city_routeids),
                    )

            # Pick up whatever landed in cache (including stale fallbacks).
            for routeid in city_routeids:
                cached = self._get_cached(routeid, allow_expired=True)
                if cached is not None:
                    self._set_cached(routeid, cached)
                    results[routeid] = cached

        # Handle routes without a known city (single-route fallback).
        for routeid in routes_needing_refresh:
            if routeid in results:
                continue
            city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
            if city is not None:
                continue  # already handled above

            static_route = self._load_static_route(routeid)
            if static_route is None:
                continue
            try:
                snapshot = self._get_single_route_snapshot(
                    routeid, static_route, force_refresh=False,
                )
                results[routeid] = snapshot
            except Exception:
                LOGGER.warning(
                    "batch single-route fallback failed routeid=%s",
                    routeid,
                )

        for canonical, aliases in aliases_by_canonical.items():
            snapshot = results.get(canonical)
            if snapshot is None:
                continue
            for alias in aliases:
                results.setdefault(alias, snapshot)

        return results

    def _apply_ntpc_eta_fallback(
        self,
        city: str,
        static_routes: dict[str, dict[str, Any]],
        items_by_route: dict[str, list[dict[str, Any]]],
        buses_by_route: dict[str, list[dict[str, Any]]],
    ) -> None:
        if city != "NewTaipei" or self.ntpc_opendata_client is None:
            return

        routeids = [
            routeid
            for routeid in static_routes
            if (
                routeid.upper().startswith("NWT")
                and not items_by_route.get(routeid)
                and buses_by_route.get(routeid)
            )
        ]
        if not routeids:
            return

        try:
            rows_by_route = self.ntpc_opendata_client.fetch_estimated_time_of_arrival_by_subroute(routeids)
        except Exception:
            LOGGER.warning(
                "NTPC OpenData ETA fallback failed routes=%s",
                len(routeids),
            )
            return

        for routeid in routeids:
            fallback_items = _build_ntpc_eta_items(
                routeid,
                static_routes[routeid],
                rows_by_route.get(routeid, []),
            )
            if fallback_items:
                items_by_route[routeid].extend(fallback_items)

    def _refresh_city_cache_for_routes(
        self,
        city: str,
        routeids: list[str],
    ) -> None:
        """Refresh the cache for *exactly* the given routeids within *city*.

        Unlike ``_refresh_city_cache`` (which uses the tracked-routes set),
        this method operates on the explicit list supplied by the caller,
        making it suitable for batch endpoint use-cases where the caller
        already knows which routes they need.
        """
        static_routes: dict[str, dict[str, Any]] = {}
        with get_connection(self.settings.db_path) as connection:
            for routeid in routeids:
                static_route = load_route_static(connection, routeid)
                if static_route is not None:
                    static_routes[routeid] = static_route

        if not static_routes:
            return

        effective_routeids = sorted(static_routes)
        stale_snapshots = {
            routeid: self._get_cached(routeid, allow_expired=True)
            for routeid in effective_routeids
        }

        resource_key = _batched_resource_key("realtime_eta", city, effective_routeids)
        with get_connection(self.settings.db_path) as connection:
            previous_state = None
            if all(snapshot is not None for snapshot in stale_snapshots.values()):
                previous_state = load_tdx_fetch_state(connection, resource_key)

            response = self.client.fetch_estimated_time_of_arrival_batch(
                city,
                effective_routeids,
                if_modified_since=None
                if previous_state is None
                else previous_state.get("last_modified"),
            )

            checked_at = int(time.time())
            _persist_fetch_state(connection, resource_key, response, checked_at, previous_state)
            _prune_old_realtime_fetch_state(connection, checked_at)
            connection.commit()

        if response.not_modified:
            LOGGER.info(
                "batch realtime not modified city=%s routes=%s",
                city,
                len(effective_routeids),
            )
            for routeid, snapshot in stale_snapshots.items():
                if snapshot is not None:
                    self._set_cached(routeid, snapshot)
            return

        LOGGER.info(
            "batch realtime refreshed city=%s routes=%s status=%s items=%s",
            city,
            len(effective_routeids),
            response.status_code,
            len(response.payload or []),
        )

        buses_response = self.client.fetch_realtime_by_frequency_batch(
            city,
            effective_routeids,
        )
        items_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in response.payload or []:
            routeid = _tdx_item_to_local(city, item, settings=self.settings)
            if routeid in static_routes:
                items_by_route[routeid].append(item)

        buses_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in buses_response.payload or []:
            routeid = _tdx_item_to_local(city, item, settings=self.settings)
            if routeid in static_routes:
                buses_by_route[routeid].append(item)
        self._apply_ntpc_eta_fallback(city, static_routes, items_by_route, buses_by_route)

        for routeid, static_route in static_routes.items():
            buses = _build_buses_payload(buses_by_route.get(routeid, []))
            if buses_response.status_code == 200 and self.route_buses_service is not None:
                self.route_buses_service.seed_cache(routeid, buses)
            snapshot = self._build_snapshot(
                routeid,
                static_route,
                items_by_route.get(routeid, []),
                realtime_buses=buses,
            )
            self._set_cached(routeid, snapshot)
            _extract_and_store_eta_travel_times(snapshot, static_route, self.settings.db_path)

    def _refresh_city_cache(
        self,
        city: str,
        requested_routeid: str,
        requested_static_route: dict[str, Any],
        *,
        force_refresh: bool,
    ) -> None:
        routeids = self._get_tracked_routeids(city, include_routeid=requested_routeid)
        static_routes = self._load_static_routes(routeids, requested_routeid, requested_static_route)
        if requested_routeid not in static_routes:
            raise RouteNotFoundError(requested_routeid)

        effective_routeids = sorted(static_routes)
        stale_snapshots = {
            routeid: self._get_cached(routeid, allow_expired=True)
            for routeid in effective_routeids
        }

        resource_key = _batched_resource_key("realtime_eta", city, effective_routeids)
        with get_connection(self.settings.db_path) as connection:
            previous_state = None
            if not force_refresh and all(snapshot is not None for snapshot in stale_snapshots.values()):
                previous_state = load_tdx_fetch_state(connection, resource_key)

            response = self.client.fetch_estimated_time_of_arrival_batch(
                city,
                effective_routeids,
                if_modified_since=None
                if force_refresh or previous_state is None
                else previous_state.get("last_modified"),
            )

            checked_at = int(time.time())
            _persist_fetch_state(connection, resource_key, response, checked_at, previous_state)
            _prune_old_realtime_fetch_state(connection, checked_at)
            connection.commit()

        if response.not_modified:
            LOGGER.info(
                "realtime batch not modified city=%s routes=%s requested_routeid=%s",
                city,
                len(effective_routeids),
                requested_routeid,
            )
            for routeid, snapshot in stale_snapshots.items():
                if snapshot is not None:
                    self._set_cached(routeid, snapshot)
            return

        LOGGER.info(
            "realtime batch refreshed city=%s routes=%s requested_routeid=%s status=%s items=%s",
            city,
            len(effective_routeids),
            requested_routeid,
            response.status_code,
            len(response.payload or []),
        )

        buses_response = self.client.fetch_realtime_by_frequency_batch(
            city,
            effective_routeids,
        )
        items_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in response.payload or []:
            routeid = _tdx_item_to_local(city, item, settings=self.settings)
            if routeid in static_routes:
                items_by_route[routeid].append(item)

        buses_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in buses_response.payload or []:
            routeid = _tdx_item_to_local(city, item, settings=self.settings)
            if routeid in static_routes:
                buses_by_route[routeid].append(item)
        self._apply_ntpc_eta_fallback(city, static_routes, items_by_route, buses_by_route)

        for routeid, static_route in static_routes.items():
            buses = _build_buses_payload(buses_by_route.get(routeid, []))
            if buses_response.status_code == 200 and self.route_buses_service is not None:
                self.route_buses_service.seed_cache(routeid, buses)
            snapshot = self._build_snapshot(
                routeid,
                static_route,
                items_by_route.get(routeid, []),
                realtime_buses=buses,
            )
            self._set_cached(routeid, snapshot)
            _extract_and_store_eta_travel_times(snapshot, static_route, self.settings.db_path)

    def _get_single_route_snapshot(
        self,
        routeid: str,
        static_route: dict[str, Any],
        *,
        force_refresh: bool,
    ) -> dict[str, Any]:
        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        try:
            items: list[dict[str, Any]] = []
            buses: list[dict[str, Any]] = []
            fetched_buses = False
            for city in self._candidate_cities_for_route(routeid):
                current_items = self.client.fetch_estimated_time_of_arrival(city, routeid)
                current_buses = self.client.fetch_realtime_by_frequency(city, routeid)
                fetched_buses = True
                if current_buses:
                    buses = _build_buses_payload(current_buses)
                if current_items:
                    items = current_items
                    break
            if (
                not items
                and buses
                and routeid.upper().startswith("NWT")
                and self.ntpc_opendata_client is not None
            ):
                rows_by_route = self.ntpc_opendata_client.fetch_estimated_time_of_arrival_by_subroute(
                    [routeid]
                )
                items = _build_ntpc_eta_items(routeid, static_route, rows_by_route.get(routeid, []))
            snapshot = self._build_snapshot(routeid, static_route, items, realtime_buses=buses)
            if fetched_buses and self.route_buses_service is not None:
                self.route_buses_service.seed_cache(routeid, buses)
        except Exception:
            stale = self._get_cached(routeid, allow_expired=True)
            if stale is not None:
                LOGGER.warning("using stale realtime cache routeid=%s after single-route refresh failure", routeid)
                self._set_cached(routeid, stale)
                return stale
            raise

        self._set_cached(routeid, snapshot)
        _extract_and_store_eta_travel_times(snapshot, static_route, self.settings.db_path)
        return snapshot

    def _candidate_cities_for_route(self, routeid: str) -> list[str]:
        candidate_cities: list[str] = []
        guessed_city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if guessed_city:
            if guessed_city == INTERCITY_CITY_NAME:
                return [guessed_city]
            candidate_cities.append(guessed_city)
        for city in self.settings.tdx_cities:
            if city not in candidate_cities:
                candidate_cities.append(city)
        return candidate_cities

    def _track_route(self, city: str, routeid: str) -> None:
        self._get_tracked_routeids(city, include_routeid=routeid)

    def _get_tracked_routeids(self, city: str, *, include_routeid: str | None = None) -> list[str]:
        now = time.monotonic()
        expires_at = now + self.settings.realtime_track_ttl

        with self._tracked_routes_lock:
            city_routes = self._tracked_routes.setdefault(city, {})
            expired_routeids = [routeid for routeid, route_expires_at in city_routes.items() if route_expires_at < now]
            for routeid in expired_routeids:
                city_routes.pop(routeid, None)

            if include_routeid:
                city_routes[include_routeid] = expires_at

            if not city_routes:
                self._tracked_routes.pop(city, None)
                return []

            return sorted(city_routes)

    def _load_static_route(self, routeid: str) -> dict[str, Any] | None:
        with get_connection(self.settings.db_path) as connection:
            return load_route_static(connection, routeid)

    def _load_static_routes(
        self,
        routeids: list[str],
        requested_routeid: str,
        requested_static_route: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        static_routes: dict[str, dict[str, Any]] = {}
        with get_connection(self.settings.db_path) as connection:
            for routeid in routeids:
                if routeid == requested_routeid:
                    static_route = requested_static_route
                else:
                    static_route = load_route_static(connection, routeid)

                if static_route is None:
                    continue
                static_routes[routeid] = static_route

        return static_routes

    def _get_city_refresh_lock(self, city: str) -> threading.Lock:
        with self._city_refresh_locks_guard:
            return self._city_refresh_locks.setdefault(city, threading.Lock())

    def _build_snapshot(
        self,
        routeid: str,
        static_route: dict[str, Any],
        items: list[dict[str, Any]],
        realtime_buses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now_ts = int(time.time())
        item_times = []
        for item in items:
            item_time = _item_updated_at(item)
            if item_time is not None:
                item_times.append(item_time)
        updated_at = max(item_times) if item_times else int(time.time())

        paths = static_route["paths"]
        grouped: dict[int, dict[str, dict[str, Any]]] = {}
        plate_candidates: dict[tuple[int, str], list[PlateObservation]] = {}
        current_native_plate_states: dict[tuple[int, str], LastNativePlateState] = {}

        for item in items:
            pathid = int(item.get("Direction") or 0)
            stopid = item.get("StopID") or item.get("StopUID")
            if not stopid:
                continue
            item_source = _eta_source(item.get("source") or item.get("_source"))
            item_estimated = bool(item.get("estimated") or item.get("_estimated"))

            path_bucket = grouped.setdefault(pathid, {})
            stop_bucket = path_bucket.setdefault(
                stopid,
                {
                    "stopid": stopid,
                    "eta": None,
                    "message": "",
                    "updated_at": None,
                    "buses": [],
                    "etas": [],
                },
            )
            item_time = _item_updated_at(item)
            _set_stop_bucket_updated_at(stop_bucket, item_time)

            estimate_time = _adjusted_eta(item, now_ts=now_ts)
            message = _build_message(item, now_ts=now_ts)
            if estimate_time is not None:
                if stop_bucket["eta"] is None or estimate_time < stop_bucket["eta"]:
                    stop_bucket["eta"] = estimate_time
                    stop_bucket["message"] = ""
            elif not stop_bucket["message"] and message:
                stop_bucket["message"] = message

            top_plate = _normalize_plate(item.get("PlateNumb"))
            top_is_arriving = (
                _to_int_or_none(item.get("VehicleStopStatus")) == ARRIVING_VEHICLE_STOP_STATUS
            )
            _append_stop_eta(
                stop_bucket,
                plate=top_plate,
                eta=estimate_time,
                is_arriving=top_is_arriving,
                source=item_source,
                estimated=item_estimated,
            )

            for estimate in item.get("Estimates") or []:
                estimate_plate = _normalize_plate(estimate.get("PlateNumb"))
                estimate_eta = _adjusted_eta(
                    estimate,
                    now_ts=now_ts,
                    fallback_update_time=item.get("SrcUpdateTime") or item.get("UpdateTime"),
                )
                estimate_is_arriving = (
                    _to_int_or_none(estimate.get("VehicleStopStatus")) == ARRIVING_VEHICLE_STOP_STATUS
                )
                _append_stop_eta(
                    stop_bucket,
                    plate=estimate_plate,
                    eta=estimate_eta,
                    is_arriving=estimate_is_arriving,
                    source=item_source,
                    estimated=item_estimated,
                )

            for observation in _collect_plate_observations(
                item,
                pathid=pathid,
                stopid=stopid,
                now_ts=now_ts,
            ):
                plate_candidates.setdefault((observation.pathid, observation.plate), []).append(observation)

        native_plates_by_path: dict[int, set[str]] = defaultdict(set)
        for pathid, plate in plate_candidates:
            native_plates_by_path[pathid].add(plate)

        for (pathid, plate), observations in plate_candidates.items():
            arriving_observations = [item for item in observations if item.is_arriving]
            effective_observations = arriving_observations or observations
            path_meta = paths.get(pathid)
            stop_index = (path_meta or {}).get("stop_index", {})

            def _rank(observation: PlateObservation) -> tuple[int, int, str]:
                eta_rank = observation.eta if observation.eta is not None else 10**9
                seq_rank = stop_index.get(observation.stopid, {}).get("seq", 10**9)
                return (eta_rank, seq_rank, observation.stopid)

            selected = min(effective_observations, key=_rank)
            path_bucket = grouped.get(pathid)
            if not path_bucket:
                continue
            stop_bucket = path_bucket.get(selected.stopid)
            if not stop_bucket:
                continue
            if all(bus["id"] != plate for bus in stop_bucket["buses"]):
                stop_bucket["buses"].append({"id": plate, "type": "normal", "source": TDX_SOURCE})
            selected_seq = _to_int_or_none(stop_index.get(selected.stopid, {}).get("seq"))
            max_seq = _max_path_stop_seq(path_meta or {})
            current_state = LastNativePlateState(
                plate=plate,
                pathid=pathid,
                stopid=selected.stopid,
                stop_seq=selected_seq,
                last_seen_ts=now_ts,
                is_terminal=(
                    selected_seq is not None and max_seq is not None and selected_seq >= max_seq
                ),
            )
            current_native_plate_states[(pathid, plate)] = current_state

        previous_native_plate_states = self._get_last_native_plate_states(routeid)
        merged_native_plate_states = dict(previous_native_plate_states)
        merged_native_plate_states.update(current_native_plate_states)
        disappeared_native_plates_by_path: dict[int, dict[str, LastNativePlateState]] = defaultdict(dict)
        for key, previous_state in previous_native_plate_states.items():
            if key in current_native_plate_states:
                continue
            if now_ts - previous_state.last_seen_ts > BACKFILL_MAX_DISAPPEARANCE_SECONDS:
                continue
            disappeared_native_plates_by_path[previous_state.pathid][previous_state.plate] = previous_state

        if realtime_buses is None and self.route_buses_service is not None:
            try:
                realtime_buses = self.route_buses_service.get_buses(routeid)
            except Exception:
                LOGGER.warning("failed to fetch realtime buses for realtime backfill routeid=%s", routeid)
                realtime_buses = []
        else:
            realtime_buses = realtime_buses or []

        _backfill_snapshot_from_buses(
            routeid=routeid,
            db_path=self.settings.db_path,
            paths=paths,
            grouped=grouped,
            native_plates_by_path=native_plates_by_path,
            disappeared_native_plates_by_path=disappeared_native_plates_by_path,
            buses=realtime_buses,
            now_ts=now_ts,
        )

        response_paths = []
        seen_pathids = set(paths)
        seen_pathids.update(grouped)

        for path_bucket in grouped.values():
            for stop_bucket in path_bucket.values():
                _finalize_stop_eta_list(stop_bucket)

        for pathid in sorted(seen_pathids):
            path_meta = paths.get(pathid)
            stop_entries = list((grouped.get(pathid) or {}).values())

            if path_meta:
                stop_index = path_meta["stop_index"]
                stop_entries.sort(
                    key=lambda item: (
                        stop_index.get(item["stopid"], {}).get("seq", 10**9),
                        item["stopid"],
                    )
                )
                path_name = path_meta["name"]
            else:
                path_name = f"Path {pathid}"
                stop_entries.sort(key=lambda item: item["stopid"])

            response_paths.append(
                {
                    "pathid": pathid,
                    "name": path_name,
                    "stops": stop_entries,
                }
            )

        snapshot = {
            "routeid": routeid,
            "updated_at": updated_at,
            "paths": response_paths,
        }
        _prune_expired_backfill(snapshot, now_ts=now_ts)
        self._set_last_native_plate_states(routeid, merged_native_plate_states)
        return snapshot

    def _get_cached(self, routeid: str, *, allow_expired: bool = False) -> dict[str, Any] | None:
        now = time.time()
        with self._cache_lock:
            entry = self._cache.get(routeid)
            if entry is None:
                return None
            if not allow_expired and entry.expires_at < now:
                return None
            snapshot = entry.snapshot
        _prune_expired_backfill(snapshot, now_ts=int(now))
        return snapshot

    def _set_cached(self, routeid: str, snapshot: dict[str, Any]) -> None:
        with self._cache_lock:
            self._cache[routeid] = CacheEntry(
                snapshot=snapshot,
                expires_at=time.time() + self.settings.realtime_cache_ttl,
            )

    def _get_last_native_plate_states(
        self,
        routeid: str,
    ) -> dict[tuple[int, str], LastNativePlateState]:
        with self._last_native_plate_states_lock:
            return dict(self._last_native_plate_states.get(routeid, {}))

    def _set_last_native_plate_states(
        self,
        routeid: str,
        states: dict[tuple[int, str], LastNativePlateState],
    ) -> None:
        cutoff = int(time.time()) - BACKFILL_MAX_DISAPPEARANCE_SECONDS
        filtered = {
            key: state
            for key, state in states.items()
            if state.last_seen_ts >= cutoff
        }
        with self._last_native_plate_states_lock:
            if filtered:
                self._last_native_plate_states[routeid] = filtered
            else:
                self._last_native_plate_states.pop(routeid, None)


class RouteBusesService:
    def __init__(self, settings: Settings, client: TDXClient) -> None:
        self.settings = settings
        self.client = client
        self._cache: dict[str, BusesCacheEntry] = {}
        self._cache_lock = threading.Lock()
        self._tracked_routes: dict[str, dict[str, float]] = {}
        self._tracked_routes_lock = threading.Lock()
        self._city_refresh_locks: dict[str, threading.Lock] = {}
        self._city_refresh_locks_guard = threading.Lock()

    def seed_cache(self, routeid: str, buses: list[dict[str, Any]]) -> None:
        """Reuse a successful realtime fetch for the route map (including empty fleets)."""
        routeid = get_route_alias_index(self.settings).canonical(routeid)
        self._set_cached(routeid, buses)

    def get_buses(self, routeid: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        routeid = get_route_alias_index(self.settings).canonical(routeid)
        if not self._route_exists(routeid):
            raise RouteNotFoundError(routeid)

        city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if city is None:
            return self._get_single_route_buses(routeid, force_refresh=force_refresh)

        self._track_route(city, routeid)

        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        city_lock = self._get_city_refresh_lock(city)
        with city_lock:
            if not force_refresh:
                cached = self._get_cached(routeid)
                if cached is not None:
                    return cached

            try:
                self._refresh_city_cache(city, routeid, force_refresh=force_refresh)
            except Exception:
                stale = self._get_cached(routeid, allow_expired=True)
                if stale is not None:
                    LOGGER.warning(
                        "using stale buses cache routeid=%s city=%s after refresh failure",
                        routeid,
                        city,
                    )
                    self._set_cached(routeid, stale)
                    return stale
                raise

            cached = self._get_cached(routeid, allow_expired=True)
            if cached is not None:
                self._set_cached(routeid, cached)
                return cached

        return []

    def _refresh_city_cache(
        self,
        city: str,
        requested_routeid: str,
        *,
        force_refresh: bool,
    ) -> None:
        routeids = self._get_tracked_routeids(city, include_routeid=requested_routeid)
        effective_routeids = sorted(routeids)
        stale_buses = {
            routeid: self._get_cached(routeid, allow_expired=True)
            for routeid in effective_routeids
        }

        resource_key = _batched_resource_key("realtime_buses", city, effective_routeids)
        with get_connection(self.settings.db_path) as connection:
            previous_state = None
            if not force_refresh and all(buses is not None for buses in stale_buses.values()):
                previous_state = load_tdx_fetch_state(connection, resource_key)

            response = self.client.fetch_realtime_by_frequency_batch(
                city,
                effective_routeids,
                if_modified_since=None
                if force_refresh or previous_state is None
                else previous_state.get("last_modified"),
            )

            checked_at = int(time.time())
            _persist_fetch_state(connection, resource_key, response, checked_at, previous_state)
            _prune_old_realtime_fetch_state(connection, checked_at)
            connection.commit()

        if response.not_modified:
            LOGGER.info(
                "realtime buses batch not modified city=%s routes=%s requested_routeid=%s",
                city,
                len(effective_routeids),
                requested_routeid,
            )
            for routeid, buses in stale_buses.items():
                if buses is not None:
                    self._set_cached(routeid, buses)
            return

        LOGGER.info(
            "realtime buses batch refreshed city=%s routes=%s requested_routeid=%s status=%s items=%s",
            city,
            len(effective_routeids),
            requested_routeid,
            response.status_code,
            len(response.payload or []),
        )

        items_by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in response.payload or []:
            routeid = _tdx_item_to_local(city, item, settings=self.settings)
            if routeid in stale_buses:
                items_by_route[routeid].append(item)

        for routeid in effective_routeids:
            self._set_cached(routeid, _build_buses_payload(items_by_route.get(routeid, [])))

    def _get_single_route_buses(self, routeid: str, *, force_refresh: bool) -> list[dict[str, Any]]:
        if not force_refresh:
            cached = self._get_cached(routeid)
            if cached is not None:
                return cached

        try:
            items: list[dict[str, Any]] = []
            for city in self._candidate_cities_for_route(routeid):
                current_items = self.client.fetch_realtime_by_frequency(city, routeid)
                if current_items:
                    items = current_items
                    break
            buses = _build_buses_payload(items)
        except Exception:
            stale = self._get_cached(routeid, allow_expired=True)
            if stale is not None:
                LOGGER.warning("using stale buses cache routeid=%s after single-route refresh failure", routeid)
                self._set_cached(routeid, stale)
                return stale
            raise

        self._set_cached(routeid, buses)
        return buses

    def _route_exists(self, routeid: str) -> bool:
        with get_connection(self.settings.db_path) as connection:
            return route_exists(connection, routeid)

    def _candidate_cities_for_route(self, routeid: str) -> list[str]:
        candidate_cities: list[str] = []
        guessed_city = guess_city_from_routeid(routeid, self.settings.tdx_cities)
        if guessed_city:
            if guessed_city == INTERCITY_CITY_NAME:
                return [guessed_city]
            candidate_cities.append(guessed_city)
        for city in self.settings.tdx_cities:
            if city not in candidate_cities:
                candidate_cities.append(city)
        return candidate_cities

    def _track_route(self, city: str, routeid: str) -> None:
        self._get_tracked_routeids(city, include_routeid=routeid)

    def _get_tracked_routeids(self, city: str, *, include_routeid: str | None = None) -> list[str]:
        now = time.monotonic()
        expires_at = now + self.settings.realtime_track_ttl

        with self._tracked_routes_lock:
            city_routes = self._tracked_routes.setdefault(city, {})
            expired_routeids = [routeid for routeid, route_expires_at in city_routes.items() if route_expires_at < now]
            for routeid in expired_routeids:
                city_routes.pop(routeid, None)

            if include_routeid:
                city_routes[include_routeid] = expires_at

            if not city_routes:
                self._tracked_routes.pop(city, None)
                return []

            return sorted(city_routes)

    def _get_city_refresh_lock(self, city: str) -> threading.Lock:
        with self._city_refresh_locks_guard:
            return self._city_refresh_locks.setdefault(city, threading.Lock())

    def _get_cached(self, routeid: str, *, allow_expired: bool = False) -> list[dict[str, Any]] | None:
        now = time.time()
        with self._cache_lock:
            entry = self._cache.get(routeid)
            if entry is None:
                return None
            if not allow_expired and entry.expires_at < now:
                return None
            return [dict(item) for item in entry.buses]

    def _set_cached(self, routeid: str, buses: list[dict[str, Any]]) -> None:
        with self._cache_lock:
            self._cache[routeid] = BusesCacheEntry(
                buses=[dict(item) for item in buses],
                expires_at=time.time() + self.settings.realtime_cache_ttl,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch a realtime route snapshot from TDX.")
    parser.add_argument("--routeid", required=True, help="TDX SubRouteUID, for example TPE307.")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.project_dir)
    settings.require_tdx_credentials()
    init_db(settings.db_path)

    token_manager = TDXTokenManager(settings)
    client = TDXClient(settings, token_manager)
    service = RealtimeService(settings, client)

    try:
        snapshot = service.get_snapshot(args.routeid, force_refresh=True)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    finally:
        client.close()
        token_manager.close()
        shutdown_logging()


if __name__ == "__main__":
    main()
