from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import CITY_PREFIX_TO_NAME
from app.logging_utils import get_logger
from app.rate_limit import enforce_rate_limit

LOGGER = get_logger("metro")

router = APIRouter(
    prefix="/api/v1/metro",
    tags=["metro"],
    dependencies=[Depends(enforce_rate_limit)],
)

# Mapping of metro system codes to TDX city names.
METRO_SYSTEMS = {
    "TRTC": {"city": "Taipei", "name": "臺北捷運", "name_en": "Taipei Metro"},
    "KRTC": {"city": "Kaohsiung", "name": "高雄捷運", "name_en": "Kaohsiung Metro"},
    "TYMC": {"city": "Taoyuan", "name": "桃園捷運", "name_en": "Taoyuan Metro"},
    "TMRT": {"city": "Taichung", "name": "臺中捷運", "name_en": "Taichung Metro"},
}

# ── Caching ──────────────────────────────────────────────────────────────────

STATIC_CACHE_TTL = 3600  # 1 hour for lines/stations (rarely change)
LIVEBOARD_CACHE_TTL = 15  # 15 seconds for realtime
TAIWAN_TZ = datetime.timezone(datetime.timedelta(hours=8))


@dataclass
class _CacheEntry:
    data: object
    fetched_at: float = field(default_factory=time.monotonic)

    def is_fresh(self, ttl: float) -> bool:
        return (time.monotonic() - self.fetched_at) < ttl


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _get_cached(key: str, ttl: float) -> object | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry.is_fresh(ttl):
            return entry.data
    return None


def _set_cached(key: str, data: object) -> None:
    with _cache_lock:
        _cache[key] = _CacheEntry(data=data)


def _get_tdx(request: Request):
    return request.app.state.tdx_client


def _taiwan_now() -> datetime.datetime:
    return datetime.datetime.now(TAIWAN_TZ)


def _estimate_minutes_to_seconds(value: object) -> int | None:
    """Convert TDX Metro LiveBoard's EstimateTime minutes to API seconds."""
    if value is None:
        return None
    try:
        return max(0, round(float(value) * 60))
    except (TypeError, ValueError):
        return None


def _normalize_liveboard(raw: list[dict]) -> list[dict]:
    entries = []
    for item in raw:
        entries.append({
            "station_id": item.get("StationID", ""),
            "station_name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "line_id": item.get("LineID", ""),
            "destination_id": item.get("DestinationStationID") or item.get("DestinationStaionID", ""),
            "destination_name": (item.get("DestinationStationName") or {}).get("Zh_tw", ""),
            "direction": item.get("Direction", 0),
            "trip_head_sign": item.get("TripHeadSign", ""),
            "train_no": item.get("TrainNo", ""),
            "estimated_time": _estimate_minutes_to_seconds(item.get("EstimateTime")),
            "service_status": item.get("ServiceStatus", 0),
        })
    return entries


_SERVICE_DAY_KEYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _runs_on_date(service_day: dict, service_date: datetime.date) -> bool:
    if not service_day:
        return True
    return bool(service_day.get(_SERVICE_DAY_KEYS[service_date.weekday()], False))


def _parse_service_time(
    value: str,
    service_date: datetime.date,
) -> datetime.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        hours, minutes = (int(part) for part in text.split(":", 1))
        if hours < 0 or minutes < 0 or minutes >= 60:
            return None
        midnight = datetime.datetime.combine(
            service_date,
            datetime.time.min,
            tzinfo=TAIWAN_TZ,
        )
        return midnight + datetime.timedelta(hours=hours, minutes=minutes)
    except (TypeError, ValueError):
        return None


def _next_timetable_arrival(
    station_timetable: dict,
    now: datetime.datetime,
) -> tuple[str, int] | None:
    candidates = []
    for service_date in (now.date() - datetime.timedelta(days=1), now.date()):
        if not _runs_on_date(station_timetable.get("service_day") or {}, service_date):
            continue
        timetables = station_timetable.get("timetables") or []
        timetables = sorted(
            enumerate(timetables),
            key=lambda indexed: (indexed[1].get("sequence", indexed[0]), indexed[0]),
        )
        previous_arrival = None
        for _, timetable in timetables:
            value = timetable.get("arrival_time") or timetable.get("departure_time") or ""
            arrival = _parse_service_time(value, service_date)
            if arrival is None:
                continue
            # TDX can publish post-midnight trips as 00:xx after 23:xx.
            while previous_arrival is not None and arrival < previous_arrival:
                arrival += datetime.timedelta(days=1)
            previous_arrival = arrival
            if arrival >= now:
                candidates.append((arrival, value))

    if not candidates:
        return None
    arrival, value = min(candidates, key=lambda candidate: candidate[0])
    return value, max(0, round((arrival - now).total_seconds()))


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/systems")
async def list_systems():
    """List supported metro systems."""
    return [
        {"system": code, **info}
        for code, info in METRO_SYSTEMS.items()
    ]


@router.get("/{system}/lines")
async def get_lines(system: str, request: Request):
    """Get all lines for a metro system."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_lines_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/Line/{system}")

    lines = []
    for item in raw:
        line_id = item.get("LineID", "")
        name_zh = (item.get("LineName") or {}).get("Zh_tw", "")
        name_en = (item.get("LineName") or {}).get("En", "")
        color = item.get("LineColor", "")
        line_no = item.get("LineNo", "")
        sections = []
        for sec in item.get("LineSectionList") or []:
            sections.append({
                "section_id": sec.get("LineSectionID", ""),
                "name": (sec.get("LineSectionName") or {}).get("Zh_tw", ""),
                "name_en": (sec.get("LineSectionName") or {}).get("En", ""),
            })
        lines.append({
            "line_id": line_id,
            "line_no": line_no,
            "name": name_zh,
            "name_en": name_en,
            "color": color,
            "sections": sections,
        })

    _set_cached(cache_key, lines)
    return lines


@router.get("/{system}/stations")
async def get_stations(system: str, request: Request):
    """Get all stations for a metro system."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_stations_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/Station/{system}")

    stations = []
    for item in raw:
        pos = item.get("StationPosition") or {}
        stations.append({
            "station_id": item.get("StationID", ""),
            "name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("StationName") or {}).get("En", ""),
            "station_address": (item.get("StationAddress") or ""),
            "line_id": item.get("LineID", ""),
            "lat": pos.get("PositionLat", 0),
            "lon": pos.get("PositionLon", 0),
        })

    _set_cached(cache_key, stations)
    return stations


@router.get("/{system}/station-of-line")
async def get_station_of_line(system: str, request: Request):
    """Get station sequences per line for a metro system."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_sol_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/StationOfLine/{system}")

    result = []
    for item in raw:
        line_id = item.get("LineID", "")
        direction = item.get("Direction", 0)
        stations_raw = item.get("Stations") or []
        stations = []
        for s in stations_raw:
            stations.append({
                "station_id": s.get("StationID", ""),
                "name": (s.get("StationName") or {}).get("Zh_tw", ""),
                "name_en": (s.get("StationName") or {}).get("En", ""),
                "sequence": s.get("Sequence", 0),
            })
        result.append({
            "line_id": line_id,
            "direction": direction,
            "stations": stations,
        })

    _set_cached(cache_key, result)
    return result


@router.get("/{system}/lines/{line_id}/liveboard")
async def get_liveboard(system: str, line_id: str, request: Request):
    """Get real-time arrival/departure info for a metro line."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_live_{system}_{line_id}"
    cached = _get_cached(cache_key, LIVEBOARD_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    try:
        raw = tdx.fetch_paginated_items(
            f"/v2/Rail/Metro/LiveBoard/{system}",
            params={"$filter": f"LineID eq '{line_id}'", "$format": "JSON"},
        )
    except Exception:
        # Some metro systems (e.g. TMRT) don't support LiveBoard on TDX.
        LOGGER.warning("TDX Metro LiveBoard unavailable for %s/%s", system, line_id, exc_info=True)
        raw = []

    entries = _normalize_liveboard(raw)

    _set_cached(cache_key, entries)
    return entries


@router.get("/{system}/lines/{line_id}/shape")
async def get_shape(system: str, line_id: str, request: Request):
    """Get route geometry for a metro line."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_shape_{system}_{line_id}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(
        f"/v2/Rail/Metro/Shape/{system}",
        params={"$filter": f"LineID eq '{line_id}'", "$format": "JSON"},
    )

    shapes = []
    for item in raw:
        geometry = item.get("Geometry", "")
        shapes.append({
            "line_id": item.get("LineID", ""),
            "direction": item.get("Direction", 0),
            "geometry": geometry,
        })

    _set_cached(cache_key, shapes)
    return shapes


@router.get("/{system}/frequency")
async def get_frequency(system: str, request: Request):
    """Get service frequency info for a metro system."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_freq_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/Frequency/{system}")

    result = []
    for item in raw:
        line_id = item.get("LineID", "")
        headways = []
        for hw in item.get("Headways") or []:
            headways.append({
                "peak_flag": hw.get("PeakFlag", ""),
                "start_time": hw.get("StartTime", ""),
                "end_time": hw.get("EndTime", ""),
                "min_headway": hw.get("MinHeadwayMins", 0),
                "max_headway": hw.get("MaxHeadwayMins", 0),
            })
        result.append({
            "line_id": line_id,
            "route_id": item.get("RouteID", ""),
            "service_day": item.get("ServiceDay", {}),
            "headways": headways,
        })

    _set_cached(cache_key, result)
    return result


@router.get("/{system}/s2s-traveltime")
async def get_s2s_traveltime(system: str, request: Request):
    """Get station-to-station travel time."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    cache_key = f"metro_s2s_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/S2STravelTime/{system}")

    result = []
    for item in raw:
        travel_times = []
        for tt in item.get("TravelTimes") or []:
            travel_times.append({
                "from_station_id": tt.get("FromStationID", ""),
                "from_station_name": (tt.get("FromStationName") or {}).get("Zh_tw", ""),
                "to_station_id": tt.get("ToStationID", ""),
                "to_station_name": (tt.get("ToStationName") or {}).get("Zh_tw", ""),
                "run_time": tt.get("RunTimeSecs", 0),
                "stop_time": tt.get("StopTimeSecs", 0),
            })
        result.append({
            "line_id": item.get("LineID", ""),
            "route_id": item.get("RouteID", ""),
            "direction": item.get("Direction", 0),
            "travel_times": travel_times,
        })

    _set_cached(cache_key, result)
    return result


# Systems that support StationTimeTable API (TMRT not supported by TDX)
TIMETABLE_SUPPORTED_SYSTEMS = {"TRTC", "KRTC", "TYMC", "KLRT"}


@router.get("/{system}/station-timetable")
async def get_station_timetable(system: str, request: Request):
    """Get station timetable for calculating ETA from schedule."""
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    if system not in TIMETABLE_SUPPORTED_SYSTEMS:
        # TMRT doesn't support StationTimeTable API on TDX
        return {"supported": False, "message": "此捷運系統不支援時刻表查詢", "data": []}

    cache_key = f"metro_timetable_{system}"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return {"supported": True, "data": cached}

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(f"/v2/Rail/Metro/StationTimeTable/{system}")

    result = []
    for item in raw:
        timetables = []
        for tt in item.get("Timetables") or []:
            timetables.append({
                "sequence": tt.get("Sequence", 0),
                "arrival_time": tt.get("ArrivalTime", ""),
                "departure_time": tt.get("DepartureTime", ""),
            })
        result.append({
            "station_id": item.get("StationID", ""),
            "station_name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "direction": item.get("Direction", 0),
            "line_id": item.get("LineID", ""),
            "destination_station_id": item.get("DestinationStationID") or item.get("DestinationStaionID", ""),
            "destination_station_name": (item.get("DestinationStationName") or {}).get("Zh_tw", ""),
            "service_day": item.get("ServiceDay") or {},
            "timetables": timetables,
        })

    _set_cached(cache_key, result)
    return {"supported": True, "data": result}


@router.get("/{system}/lines/{line_id}/eta")
async def get_line_eta(system: str, line_id: str, request: Request):
    """Get calculated ETA for each station based on timetable.

    For systems with LiveBoard (TRTC, KRTC, TYMC), prefer LiveBoard.
    For systems without LiveBoard data, calculate from StationTimeTable.
    """
    system = system.upper()
    if system not in METRO_SYSTEMS:
        raise HTTPException(404, f"Unknown metro system: {system}")

    now = _taiwan_now()
    current_time = now.strftime("%H:%M")

    # Try LiveBoard first
    liveboard_cache_key = f"metro_live_{system}_{line_id}"
    liveboard_cached = _get_cached(liveboard_cache_key, LIVEBOARD_CACHE_TTL)

    tdx = _get_tdx(request)
    liveboard = []

    if liveboard_cached is not None:
        liveboard = liveboard_cached
    elif system in TIMETABLE_SUPPORTED_SYSTEMS:
        try:
            raw = tdx.fetch_paginated_items(
                f"/v2/Rail/Metro/LiveBoard/{system}",
                params={"$filter": f"LineID eq '{line_id}'", "$format": "JSON"},
            )
            liveboard = _normalize_liveboard(raw)
            _set_cached(liveboard_cache_key, liveboard)
        except Exception:
            LOGGER.warning("TDX Metro LiveBoard unavailable for %s/%s", system, line_id, exc_info=True)
            liveboard = []

    # Check if LiveBoard has meaningful data (not all zeros)
    liveboard_useful = any(
        e.get("estimated_time") is not None and e.get("estimated_time") > 0
        for e in liveboard
    )

    if liveboard_useful:
        return {
            "source": "liveboard",
            "current_time": current_time,
            "entries": liveboard,
        }

    # Fall back to timetable calculation
    if system not in TIMETABLE_SUPPORTED_SYSTEMS:
        # For TMRT, return frequency info instead
        freq_data = await get_frequency(system, request)
        line_freq = [f for f in freq_data if f.get("line_id") == line_id]
        return {
            "source": "frequency",
            "current_time": current_time,
            "message": "此捷運系統無即時資訊，顯示班距參考",
            "frequency": line_freq,
            "entries": [],
        }

    # Calculate ETA from timetable
    timetable_response = await get_station_timetable(system, request)
    if not timetable_response.get("supported"):
        return {
            "source": "none",
            "current_time": current_time,
            "entries": [],
        }

    timetable_data = timetable_response.get("data", [])
    # Filter to this line
    line_timetables = [t for t in timetable_data if t.get("line_id") == line_id]

    entries_by_trip = {}
    for station_tt in line_timetables:
        station_id = station_tt.get("station_id", "")
        station_name = station_tt.get("station_name", "")
        direction = station_tt.get("direction", 0)
        dest_id = station_tt.get("destination_station_id", "")
        dest_name = station_tt.get("destination_station_name", "")
        next_arrival = _next_timetable_arrival(station_tt, now)
        if next_arrival is None:
            continue
        next_arrival_text, eta_seconds = next_arrival
        entry = {
            "station_id": station_id,
            "station_name": station_name,
            "line_id": line_id,
            "direction": direction,
            "destination_id": dest_id,
            "destination_name": dest_name,
            "trip_head_sign": f"往{dest_name}" if dest_name else "",
            "estimated_time": eta_seconds,
            "next_arrival": next_arrival_text,
            "service_status": 0,
        }
        key = (station_id, direction, dest_id or dest_name)
        previous = entries_by_trip.get(key)
        if previous is None or eta_seconds < previous["estimated_time"]:
            entries_by_trip[key] = entry

    entries = list(entries_by_trip.values())

    return {
        "source": "timetable",
        "current_time": current_time,
        "entries": entries,
    }
