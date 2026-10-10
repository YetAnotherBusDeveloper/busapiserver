from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.logging_utils import get_logger
from app.rate_limit import enforce_rate_limit
from app.singleflight import singleflight

LOGGER = get_logger("bike")

router = APIRouter(
    prefix="/api/v1/bike",
    tags=["bike"],
    dependencies=[Depends(enforce_rate_limit)],
)

# Cities that have YouBike / public bike systems.
BIKE_CITIES = {
    "Taipei": "臺北市",
    "NewTaipei": "新北市",
    "Taoyuan": "桃園市",
    "Hsinchu": "新竹市",
    "HsinchuCounty": "新竹縣",
    "Taichung": "臺中市",
    "Chiayi": "嘉義市",
    "Tainan": "臺南市",
    "Kaohsiung": "高雄市",
    "PingtungCounty": "屏東縣",
}

# ── Caching ──────────────────────────────────────────────────────────────────

STATION_CACHE_TTL = 3600  # 1 hour for station list (positions rarely change)
AVAILABILITY_CACHE_TTL = 20  # 20 seconds for realtime availability


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


# ── Helpers ──────────────────────────────────────────────────────────────────

@singleflight
def _fetch_stations_raw(tdx, city: str) -> list[dict]:
    cache_key = f"bike_stations_raw_{city}"
    cached = _get_cached(cache_key, STATION_CACHE_TTL)
    if cached is not None:
        return cached
    raw = tdx.fetch_paginated_items(f"/v2/Bike/Station/City/{city}")
    _set_cached(cache_key, raw)
    return raw


@singleflight
def _fetch_availability_raw(tdx, city: str) -> list[dict]:
    cache_key = f"bike_avail_raw_{city}"
    cached = _get_cached(cache_key, AVAILABILITY_CACHE_TTL)
    if cached is not None:
        return cached
    raw = tdx.fetch_paginated_items(f"/v2/Bike/Availability/City/{city}")
    _set_cached(cache_key, raw)
    return raw


def _parse_station(item: dict) -> dict:
    pos = item.get("StationPosition") or {}
    return {
        "station_uid": item.get("StationUID", ""),
        "station_id": item.get("StationID", ""),
        "name": (item.get("StationName") or {}).get("Zh_tw", ""),
        "name_en": (item.get("StationName") or {}).get("En", ""),
        "address": (item.get("StationAddress") or {}).get("Zh_tw", ""),
        "lat": pos.get("PositionLat", 0),
        "lon": pos.get("PositionLon", 0),
        "service_type": item.get("ServiceType", 0),
    }


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6378137.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/cities")
async def list_cities():
    """List cities with bike sharing service."""
    return [
        {"city": code, "name": name}
        for code, name in BIKE_CITIES.items()
    ]


@router.get("/stations")
@singleflight
def get_stations(
    request: Request,
    city: str = Query(..., description="City name, e.g. Taipei"),
    lat: float = Query(0, description="Latitude for distance sorting"),
    lon: float = Query(0, description="Longitude for distance sorting"),
    radius: int = Query(0, description="Radius in meters (0 = all)"),
):
    """Get bike stations with real-time availability for a city."""
    if city not in BIKE_CITIES:
        raise HTTPException(404, f"City not supported: {city}")

    tdx = _get_tdx(request)
    stations_raw = _fetch_stations_raw(tdx, city)
    avail_raw = _fetch_availability_raw(tdx, city)

    # Build availability lookup
    avail_map: dict[str, dict] = {}
    for a in avail_raw:
        uid = a.get("StationUID", "")
        avail_map[uid] = {
            "available_rent": a.get("AvailableRentBikes", 0),
            "available_rent_general": a.get("AvailableRentBikesDetail", {}).get("GeneralBikes", 0),
            "available_rent_electric": a.get("AvailableRentBikesDetail", {}).get("ElectricBikes", 0),
            "available_return": a.get("AvailableReturnBikes", 0),
            "service_status": a.get("ServiceStatus", 0),
            "update_time": a.get("SrcUpdateTime", ""),
        }

    result = []
    has_position = lat != 0 or lon != 0
    for s in stations_raw:
        station = _parse_station(s)
        uid = station["station_uid"]
        avail = avail_map.get(uid, {})
        entry = {**station, **avail}

        if has_position:
            dist = _haversine_meters(lat, lon, station["lat"], station["lon"])
            entry["distance_meters"] = round(dist)
            if radius > 0 and dist > radius:
                continue
        result.append(entry)

    if has_position:
        result.sort(key=lambda x: x.get("distance_meters", float("inf")))

    return result


@router.get("/nearby")
@singleflight
def get_nearby(
    request: Request,
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    radius: int = Query(500, description="Search radius in meters"),
):
    """Find nearby bike stations across all cities (auto-detect city)."""
    tdx = _get_tdx(request)

    # Determine best city by checking which city center is nearest
    import math
    _city_centers = {
        "Taipei": (25.0330, 121.5654),
        "NewTaipei": (25.0119, 121.4638),
        "Taoyuan": (24.9937, 121.3010),
        "Hsinchu": (24.8042, 120.9717),
        "HsinchuCounty": (24.8396, 121.0047),
        "Taichung": (24.1477, 120.6736),
        "Chiayi": (23.4801, 120.4491),
        "Tainan": (22.9999, 120.2269),
        "Kaohsiung": (22.6273, 120.3014),
        "PingtungCounty": (22.5519, 120.5487),
    }

    best_city = min(
        _city_centers,
        key=lambda c: _haversine_meters(lat, lon, *_city_centers[c]),
    )

    stations_raw = _fetch_stations_raw(tdx, best_city)
    avail_raw = _fetch_availability_raw(tdx, best_city)

    avail_map: dict[str, dict] = {}
    for a in avail_raw:
        uid = a.get("StationUID", "")
        avail_map[uid] = {
            "available_rent": a.get("AvailableRentBikes", 0),
            "available_rent_general": a.get("AvailableRentBikesDetail", {}).get("GeneralBikes", 0),
            "available_rent_electric": a.get("AvailableRentBikesDetail", {}).get("ElectricBikes", 0),
            "available_return": a.get("AvailableReturnBikes", 0),
            "service_status": a.get("ServiceStatus", 0),
            "update_time": a.get("SrcUpdateTime", ""),
        }

    result = []
    for s in stations_raw:
        station = _parse_station(s)
        dist = _haversine_meters(lat, lon, station["lat"], station["lon"])
        if dist > radius:
            continue
        uid = station["station_uid"]
        avail = avail_map.get(uid, {})
        result.append({**station, **avail, "distance_meters": round(dist), "city": best_city})

    result.sort(key=lambda x: x["distance_meters"])
    return result
