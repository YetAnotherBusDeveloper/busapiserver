from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.logging_utils import get_logger
from app.rate_limit import enforce_rate_limit

LOGGER = get_logger("rail")

router = APIRouter(
    prefix="/api/v1",
    tags=["rail"],
    dependencies=[Depends(enforce_rate_limit)],
)

# ── Caching ──────────────────────────────────────────────────────────────────

STATIC_CACHE_TTL = 3600
REALTIME_CACHE_TTL = 30
TIMETABLE_CACHE_TTL = 300  # 5 min for daily timetables
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


def _parse_hhmm(value: str, service_date: datetime.date) -> datetime.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        hours, minutes = text.split(":", 1)
        return datetime.datetime(
            service_date.year,
            service_date.month,
            service_date.day,
            int(hours),
            int(minutes),
            tzinfo=TAIWAN_TZ,
        )
    except (TypeError, ValueError):
        return None


def _roll_forward(
    current: datetime.datetime | None,
    previous: datetime.datetime | None,
) -> datetime.datetime | None:
    if current is None:
        return None
    if previous is None:
        return current
    while current < previous:
        current += datetime.timedelta(days=1)
    return current


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _lerp(start: float, end: float, ratio: float) -> float:
    return start + ((end - start) * ratio)


def _get_tra_station_lookup(request: Request) -> dict[str, dict[str, object]]:
    cache_key = "tra_station_lookup"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/Station")

    lookup: dict[str, dict[str, object]] = {}
    for item in raw:
        station_id = item.get("StationID", "")
        if not station_id:
            continue
        position = item.get("StationPosition") or {}
        lookup[station_id] = {
            "station_id": station_id,
            "name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("StationName") or {}).get("En", ""),
            "lat": position.get("PositionLat", 0) or 0,
            "lon": position.get("PositionLon", 0) or 0,
        }

    _set_cached(cache_key, lookup)
    return lookup


def _get_tra_train_timetable(
    request: Request,
    *,
    train_no: str,
    service_date: datetime.date,
) -> dict[str, object] | None:
    cache_key = f"tra_train_timetable_{train_no}_{service_date.isoformat()}"
    cached = _get_cached(cache_key, TIMETABLE_CACHE_TTL)
    if cached is not None:
        return cached  # type: ignore[return-value]

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(
        f"/v2/Rail/TRA/DailyTimetable/TrainNo/{train_no}/TrainDate/{service_date.isoformat()}"
    )
    timetable = raw[0] if raw else None
    if timetable is not None:
        _set_cached(cache_key, timetable)
    return timetable


def _build_train_position_payload(
    *,
    live_entry: dict[str, object],
    timetable: dict[str, object],
    station_lookup: dict[str, dict[str, object]],
    service_date: datetime.date,
    now: datetime.datetime,
) -> dict[str, object] | None:
    daily_info = timetable.get("DailyTrainInfo") or {}
    stop_times = timetable.get("StopTimes") or []
    if not stop_times:
        return None

    normalized_stops: list[dict[str, object]] = []
    previous_time: datetime.datetime | None = None

    for raw_stop in stop_times:
        station_id = raw_stop.get("StationID", "")
        station_name = (raw_stop.get("StationName") or {}).get("Zh_tw", "")
        station_meta = station_lookup.get(station_id, {})
        arrival = _roll_forward(
            _parse_hhmm(raw_stop.get("ArrivalTime", ""), service_date),
            previous_time,
        )
        if arrival is not None:
            previous_time = arrival
        departure = _roll_forward(
            _parse_hhmm(raw_stop.get("DepartureTime", ""), service_date),
            previous_time,
        )
        if departure is not None:
            previous_time = departure
        normalized_stops.append({
            "station_id": station_id,
            "station_name": station_name or station_meta.get("name", ""),
            "arrival": arrival,
            "departure": departure,
            "lat": float(station_meta.get("lat", 0) or 0),
            "lon": float(station_meta.get("lon", 0) or 0),
            "sequence": raw_stop.get("StopSequence", 0) or 0,
        })

    delay_minutes = int(live_entry.get("DelayTime") or 0)
    delay_delta = datetime.timedelta(minutes=delay_minutes)
    adjusted_now = now

    first_stop = normalized_stops[0]
    first_departure = first_stop.get("departure") or first_stop.get("arrival")
    if isinstance(first_departure, datetime.datetime) and adjusted_now <= first_departure + delay_delta:
        next_stop = normalized_stops[1] if len(normalized_stops) > 1 else first_stop
        return _finalize_train_position(
            live_entry=live_entry,
            daily_info=daily_info,
            current_stop=first_stop,
            next_stop=next_stop,
            progress=0.0,
            status="at_station",
        )

    for index, current_stop in enumerate(normalized_stops):
        arrival = current_stop.get("arrival")
        departure = current_stop.get("departure") or arrival
        if not isinstance(departure, datetime.datetime):
            continue
        if not isinstance(arrival, datetime.datetime):
            arrival = departure
        adjusted_arrival = arrival + delay_delta
        adjusted_departure = departure + delay_delta
        next_stop = normalized_stops[index + 1] if index + 1 < len(normalized_stops) else None

        if adjusted_arrival <= adjusted_now <= adjusted_departure:
            return _finalize_train_position(
                live_entry=live_entry,
                daily_info=daily_info,
                current_stop=current_stop,
                next_stop=next_stop or current_stop,
                progress=0.0,
                status="at_station",
            )

        if next_stop is None:
            continue

        next_arrival = next_stop.get("arrival") or next_stop.get("departure")
        if not isinstance(next_arrival, datetime.datetime):
            continue
        segment_start = adjusted_departure
        segment_end = next_arrival + delay_delta
        if segment_end <= segment_start:
            continue
        if segment_start <= adjusted_now <= segment_end:
            elapsed = (adjusted_now - segment_start).total_seconds()
            total = (segment_end - segment_start).total_seconds()
            progress = _clamp(elapsed / total if total > 0 else 0.0)
            return _finalize_train_position(
                live_entry=live_entry,
                daily_info=daily_info,
                current_stop=current_stop,
                next_stop=next_stop,
                progress=progress,
                status="between_stations",
            )

    last_stop = normalized_stops[-1]
    return _finalize_train_position(
        live_entry=live_entry,
        daily_info=daily_info,
        current_stop=last_stop,
        next_stop=last_stop,
        progress=1.0,
        status="arrived",
    )


def _finalize_train_position(
    *,
    live_entry: dict[str, object],
    daily_info: dict[str, object],
    current_stop: dict[str, object],
    next_stop: dict[str, object],
    progress: float,
    status: str,
) -> dict[str, object] | None:
    current_lat = float(current_stop.get("lat", 0) or 0)
    current_lon = float(current_stop.get("lon", 0) or 0)
    next_lat = float(next_stop.get("lat", 0) or 0)
    next_lon = float(next_stop.get("lon", 0) or 0)

    if status == "between_stations":
        if (current_lat == 0 and current_lon == 0) or (next_lat == 0 and next_lon == 0):
            return None
        latitude = _lerp(current_lat, next_lat, progress)
        longitude = _lerp(current_lon, next_lon, progress)
    else:
        if current_lat == 0 and current_lon == 0:
            return None
        latitude = current_lat
        longitude = current_lon

    return {
        "train_no": live_entry.get("TrainNo", ""),
        "train_type": (live_entry.get("TrainTypeName") or {}).get("Zh_tw", ""),
        "direction": live_entry.get("Direction", 0),
        "starting_station_name": (daily_info.get("StartingStationName") or {}).get("Zh_tw", ""),
        "ending_station_name": (daily_info.get("EndingStationName") or {}).get("Zh_tw", ""),
        "delay_minutes": int(live_entry.get("DelayTime") or 0),
        "status": status,
        "progress": round(_clamp(progress), 4),
        "current_station_id": current_stop.get("station_id", ""),
        "current_station_name": current_stop.get("station_name", ""),
        "next_station_id": next_stop.get("station_id", ""),
        "next_station_name": next_stop.get("station_name", ""),
        "lat": round(latitude, 6),
        "lon": round(longitude, 6),
        "updated_at": live_entry.get("UpdateTime") or live_entry.get("SrcUpdateTime") or "",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  THSR (Taiwan High Speed Rail)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/thsr/stations")
async def thsr_stations(request: Request):
    """Get all THSR stations."""
    cache_key = "thsr_stations"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/THSR/Station")

    stations = []
    for item in raw:
        pos = item.get("StationPosition") or {}
        stations.append({
            "station_id": item.get("StationID", ""),
            "name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("StationName") or {}).get("En", ""),
            "station_class": item.get("StationClass", ""),
            "lat": pos.get("PositionLat", 0),
            "lon": pos.get("PositionLon", 0),
        })

    _set_cached(cache_key, stations)
    return stations


@router.get("/thsr/timetable/od")
async def thsr_timetable_od(
    request: Request,
    origin: str = Query(..., description="Origin station ID"),
    dest: str = Query(..., description="Destination station ID"),
    date: str = Query("", description="Date in YYYY-MM-DD, empty for today"),
):
    """Query THSR OD timetable."""
    cache_key = f"thsr_od_{origin}_{dest}_{date}"
    cached = _get_cached(cache_key, TIMETABLE_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    if not date:
        date = datetime.date.today().isoformat()
    path = f"/v2/Rail/THSR/DailyTimetable/OD/{origin}/to/{dest}/{date}"
    raw = tdx.fetch_paginated_items(path)

    trains = []
    for item in raw:
        train_info = item.get("DailyTrainInfo") or {}
        origin_stop = item.get("OriginStopTime") or {}
        dest_stop = item.get("DestinationStopTime") or {}
        trains.append({
            "train_no": train_info.get("TrainNo", ""),
            "direction": train_info.get("Direction", 0),
            "start_station": (train_info.get("StartingStationName") or {}).get("Zh_tw", ""),
            "end_station": (train_info.get("EndingStationName") or {}).get("Zh_tw", ""),
            "origin_departure": origin_stop.get("DepartureTime", ""),
            "dest_arrival": dest_stop.get("ArrivalTime", ""),
            "origin_station_id": origin_stop.get("StationID", ""),
            "dest_station_id": dest_stop.get("StationID", ""),
        })

    _set_cached(cache_key, trains)
    return trains


@router.get("/thsr/timetable/today")
async def thsr_timetable_today(request: Request):
    """Get all THSR trains today."""
    cache_key = "thsr_today"
    cached = _get_cached(cache_key, TIMETABLE_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/THSR/DailyTimetable/Today")

    trains = []
    for item in raw:
        train_info = item.get("DailyTrainInfo") or {}
        stop_times = []
        for st in item.get("StopTimes") or []:
            stop_times.append({
                "station_id": st.get("StationID", ""),
                "station_name": (st.get("StationName") or {}).get("Zh_tw", ""),
                "arrival": st.get("ArrivalTime", ""),
                "departure": st.get("DepartureTime", ""),
            })
        trains.append({
            "train_no": train_info.get("TrainNo", ""),
            "direction": train_info.get("Direction", 0),
            "start_station": (train_info.get("StartingStationName") or {}).get("Zh_tw", ""),
            "end_station": (train_info.get("EndingStationName") or {}).get("Zh_tw", ""),
            "stop_times": stop_times,
        })

    _set_cached(cache_key, trains)
    return trains


@router.get("/thsr/seats/{station_id}")
async def thsr_seats(station_id: str, request: Request):
    """Get real-time seat availability at a THSR station."""
    cache_key = f"thsr_seats_{station_id}"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    # TDX returns a single object {AvailableSeats: [...], UpdateTime: ...},
    # not a paginated list, so we use _request_json directly.
    payload = tdx._request_json(
        f"/v2/Rail/THSR/AvailableSeatStatusList/{station_id}",
        params={"$format": "JSON"},
    )
    available = []
    if isinstance(payload, dict):
        available = payload.get("AvailableSeats") or []

    result = []
    for train in available:
            cars = []
            for car in train.get("StopStations") or []:
                cars.append({
                    "station_id": car.get("StationID", ""),
                    "station_name": (car.get("StationName") or {}).get("Zh_tw", ""),
                    "standard_seat": car.get("StandardSeatStatus", ""),
                    "business_seat": car.get("BusinessSeatStatus", ""),
                })
            result.append({
                "train_no": train.get("TrainNo", ""),
                "direction": train.get("Direction", 0),
                "departure_time": train.get("DepartureTime", ""),
                "destination": (train.get("EndingStationName") or {}).get("Zh_tw", ""),
                "seat_info": cars,
            })

    _set_cached(cache_key, result)
    return result


@router.get("/thsr/alerts")
async def thsr_alerts(request: Request):
    """Get THSR operational alerts."""
    cache_key = "thsr_alerts"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/THSR/AlertInfo")

    alerts = _parse_rail_alerts(raw)
    _set_cached(cache_key, alerts)
    return alerts


@router.get("/thsr/shape")
async def thsr_shape(request: Request):
    """Get THSR route geometry."""
    cache_key = "thsr_shape"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/THSR/Shape")

    shapes = []
    for item in raw:
        shapes.append({
            "line_name": (item.get("LineName") or {}).get("Zh_tw", ""),
            "geometry": item.get("Geometry", ""),
        })

    _set_cached(cache_key, shapes)
    return shapes


# ══════════════════════════════════════════════════════════════════════════════
#  TRA (Taiwan Railway Administration)
# ══════════════════════════════════════════════════════════════════════════════

# TDX returns no usable line names for TRA: /v2/Rail/TRA/Line comes back with
# LineName and LineNo empty on every row, and StationOfLine carries no name at
# all. These are mapped from each line's observed first/last station, since the
# ids are not always the mnemonic you would guess -- SA runs 瑞芳→八斗子 (深澳線)
# and SH runs 中洲→沙崙 (沙崙線), not the other way round.
TRA_LINE_NAMES = {
    "WL": "西部幹線",    # 基隆 → 屏東
    "WL-C": "海線",      # 竹南 → 彰化
    "EL": "東部幹線",    # 八堵 → 臺東
    "SL": "南迴線",      # 屏東 → 臺東
    "PX": "平溪線",      # 三貂嶺 → 菁桐
    "NW": "內灣線",      # 北新竹 → 內灣
    "JJ": "集集線",      # 二水 → 車埕
    "SA": "深澳線",      # 瑞芳 → 八斗子
    "SH": "沙崙線",      # 中洲 → 沙崙
    "LJ": "六家線",      # 竹中 → 六家
    "SU": "蘇澳線",      # 蘇澳新 → 蘇澳
    "CZ": "成追線",      # 成功 → 追分
}


@router.get("/tra/stations")
async def tra_stations(request: Request):
    """Get all TRA stations."""
    cache_key = "tra_stations"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/Station")

    stations = []
    for item in raw:
        pos = item.get("StationPosition") or {}
        stations.append({
            "station_id": item.get("StationID", ""),
            "name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("StationName") or {}).get("En", ""),
            "station_class": item.get("StationClass", ""),
            "lat": pos.get("PositionLat", 0),
            "lon": pos.get("PositionLon", 0),
        })

    _set_cached(cache_key, stations)
    return stations


@router.get("/tra/lines")
async def tra_lines(request: Request):
    """Get all TRA lines."""
    cache_key = "tra_lines"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/Line")

    lines = []
    for item in raw:
        lines.append({
            "line_id": item.get("LineID", ""),
            "line_no": item.get("LineNo", ""),
            "name": (item.get("LineName") or {}).get("Zh_tw", ""),
            "name_en": (item.get("LineName") or {}).get("En", ""),
        })

    _set_cached(cache_key, lines)
    return lines


@router.get("/tra/station-of-line")
async def tra_station_of_line(request: Request):
    """Get the ordered station list of every TRA line."""
    cache_key = "tra_station_of_line"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/StationOfLine")

    lines = []
    for item in raw:
        line_id = item.get("LineID", "")
        stations = []
        for stop in item.get("Stations") or []:
            stations.append({
                "station_id": stop.get("StationID", ""),
                # Unlike /Station and every Metro resource, TRA's StationOfLine
                # returns StationName as a bare string, not a {Zh_tw, En} object.
                "name": stop.get("StationName") or "",
                "sequence": stop.get("Sequence") or 0,
            })
        stations.sort(key=lambda s: s["sequence"])
        lines.append({
            "line_id": line_id,
            # TDX supplies no line names for TRA -- both StationOfLine and
            # /v2/Rail/TRA/Line come back with LineName/LineNo empty -- so the
            # Chinese names are mapped here, falling back to the raw id.
            "line_name": TRA_LINE_NAMES.get(line_id, line_id),
            "stations": stations,
        })

    _set_cached(cache_key, lines)
    return lines


@router.get("/tra/timetable/od")
async def tra_timetable_od(
    request: Request,
    origin: str = Query(..., description="Origin station ID"),
    dest: str = Query(..., description="Destination station ID"),
    date: str = Query("", description="Date YYYY-MM-DD, empty for today"),
):
    """Query TRA OD timetable."""
    cache_key = f"tra_od_{origin}_{dest}_{date}"
    cached = _get_cached(cache_key, TIMETABLE_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    if not date:
        date = datetime.date.today().isoformat()
    path = f"/v2/Rail/TRA/DailyTimetable/OD/{origin}/to/{dest}/{date}"
    raw = tdx.fetch_paginated_items(path)

    trains = []
    for item in raw:
        train_info = item.get("DailyTrainInfo") or {}
        origin_stop = item.get("OriginStopTime") or {}
        dest_stop = item.get("DestinationStopTime") or {}
        train_type = (train_info.get("TrainTypeName") or {}).get("Zh_tw", "")
        trains.append({
            "train_no": train_info.get("TrainNo", ""),
            "train_type": train_type,
            "direction": train_info.get("Direction", 0),
            "start_station": (train_info.get("StartingStationName") or {}).get("Zh_tw", ""),
            "end_station": (train_info.get("EndingStationName") or {}).get("Zh_tw", ""),
            "origin_departure": origin_stop.get("DepartureTime", ""),
            "dest_arrival": dest_stop.get("ArrivalTime", ""),
        })

    _set_cached(cache_key, trains)
    return trains


@router.get("/tra/liveboard/{station_id}")
async def tra_liveboard(station_id: str, request: Request):
    """Get real-time arrival/departure at a TRA station."""
    cache_key = f"tra_live_{station_id}"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items(
        f"/v2/Rail/TRA/LiveBoard/Station/{station_id}",
    )

    entries = []
    for item in raw:
        train_type = (item.get("TrainTypeName") or {}).get("Zh_tw", "")
        entries.append({
            "train_no": item.get("TrainNo", ""),
            "train_type": train_type,
            "station_id": item.get("StationID", ""),
            "station_name": (item.get("StationName") or {}).get("Zh_tw", ""),
            "end_station": (item.get("EndingStationName") or {}).get("Zh_tw", ""),
            "direction": item.get("Direction", 0),
            "scheduled_arrival": item.get("ScheduledArrivalTime", ""),
            "scheduled_departure": item.get("ScheduledDepartureTime", ""),
            "delay_minutes": item.get("DelayTime", 0),
        })

    _set_cached(cache_key, entries)
    return entries


@router.get("/tra/train-positions/{station_id}")
async def tra_train_positions(
    station_id: str,
    request: Request,
    limit: int = Query(8, ge=1, le=20, description="Maximum trains to estimate"),
):
    """Estimate TRA train positions near a station using liveboard plus today's timetable."""
    cache_key = f"tra_train_positions_{station_id}_{limit}"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    liveboard = tdx.fetch_paginated_items(f"/v2/Rail/TRA/LiveBoard/Station/{station_id}")
    if not liveboard:
        _set_cached(cache_key, [])
        return []

    station_lookup = _get_tra_station_lookup(request)
    now = _taiwan_now()
    service_date = now.date()

    positions: list[dict[str, object]] = []
    for entry in liveboard[:limit]:
        train_no = str(entry.get("TrainNo") or "").strip()
        if not train_no:
            continue
        try:
            timetable = _get_tra_train_timetable(
                request,
                train_no=train_no,
                service_date=service_date,
            )
        except Exception as exc:
            LOGGER.warning("failed to fetch TRA timetable for %s: %s", train_no, exc)
            continue
        if not timetable:
            continue
        position = _build_train_position_payload(
            live_entry=entry,
            timetable=timetable,
            station_lookup=station_lookup,
            service_date=service_date,
            now=now,
        )
        if position is not None:
            positions.append(position)

    _set_cached(cache_key, positions)
    return positions


@router.get("/tra/shape")
async def tra_shape(request: Request):
    """Get TRA route geometry."""
    cache_key = "tra_shape"
    cached = _get_cached(cache_key, STATIC_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/Shape")

    shapes = []
    for item in raw:
        shapes.append({
            "line_id": item.get("LineID", ""),
            "line_name": (item.get("LineName") or {}).get("Zh_tw", ""),
            "geometry": item.get("Geometry", ""),
        })

    _set_cached(cache_key, shapes)
    return shapes


@router.get("/tra/alerts")
async def tra_alerts(request: Request):
    """Get TRA operational alerts (derived from LiveTrainDelay)."""
    cache_key = "tra_alerts"
    cached = _get_cached(cache_key, REALTIME_CACHE_TTL)
    if cached is not None:
        return cached

    tdx = _get_tdx(request)
    raw = tdx.fetch_paginated_items("/v2/Rail/TRA/LiveTrainDelay")

    # Summarise delayed trains as alerts.
    delayed = [d for d in raw if (d.get("DelayTime") or 0) > 0]
    alerts = []
    if delayed:
        summary_lines = []
        for d in delayed[:20]:  # cap at 20
            name = (d.get("StationName") or {}).get("Zh_tw", "")
            summary_lines.append(
                f"車次 {d.get('TrainNo','')} 於 {name} 延誤 {d.get('DelayTime',0)} 分鐘"
            )
        alerts.append({
            "alert_id": "tra_delay_summary",
            "title": f"目前有 {len(delayed)} 班列車延誤",
            "description": "\n".join(summary_lines),
            "status": 0,
            "scope": "",
            "direction": None,
            "publish_time": "",
            "update_time": delayed[0].get("UpdateTime", "") if delayed else "",
            "start_time": "",
            "end_time": "",
        })

    _set_cached(cache_key, alerts)
    return alerts


# ══════════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_rail_alerts(raw: list[dict]) -> list[dict]:
    alerts = []
    for item in raw:
        # Title / Description may be plain strings or NameType dicts.
        title_raw = item.get("Title") or ""
        if isinstance(title_raw, dict):
            title_raw = title_raw.get("Zh_tw") or title_raw.get("En") or ""
        desc_raw = item.get("Description") or ""
        if isinstance(desc_raw, dict):
            desc_raw = desc_raw.get("Zh_tw") or desc_raw.get("En") or ""
        title_text = str(title_raw).strip()
        desc_text = str(desc_raw).strip()
        normalized_title = title_text.replace(" ", "").lower()
        normalized_desc = desc_text.replace(" ", "").lower()
        alert_id = str(item.get("AlertID", "") or "")
        if (
            normalized_title in {"全線營運正常", "全線營運正常(normal)", "normal"}
            or (
                alert_id == "00000000-0000-0000-0000-000000000000"
                and "全線營運正常" in title_text
            )
            or normalized_desc in {"全線營運正常", "全線營運正常(normal)", "normal"}
        ):
            continue
        raw_status = item.get("Status", 0)
        try:
            status_int = int(raw_status) if raw_status else 0
        except (TypeError, ValueError):
            status_int = 0
        alerts.append({
            "alert_id": alert_id,
            "title": title_text,
            "description": desc_text,
            "status": status_int,
            "scope": item.get("Scope", ""),
            "direction": item.get("Direction"),
            "publish_time": item.get("PublishTime", ""),
            "update_time": item.get("UpdateTime", ""),
            "start_time": item.get("StartTime", ""),
            "end_time": item.get("EndTime", ""),
        })
    return alerts
