from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import threading
import time

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from app.db import get_connection, load_database_version, load_path_points, load_route_static, path_exists, route_exists
from app.config import CITY_NAME_TO_PREFIX, CITY_PREFIX_TO_NAME, guess_city_from_routeid
from app.logging_utils import get_logger
from app.rate_limit import enforce_rate_limit
from app.route_aliases import get_route_alias_index
from app.sync_realtime import RouteNotFoundError

LOGGER = get_logger("routes")
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


router = APIRouter(tags=["Bus"], dependencies=[Depends(enforce_rate_limit)])

_download_name_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

ALERTS_CACHE_TTL_SECONDS = 600  # 10 minutes


@dataclass
class _AlertsCacheEntry:
    alerts: list[dict]
    fetched_at: float = field(default_factory=time.monotonic)

    @property
    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < ALERTS_CACHE_TTL_SECONDS


_alerts_cache: dict[str, _AlertsCacheEntry] = {}
_alerts_cache_lock = threading.Lock()
_alerts_in_flight: dict[str, list[dict] | None] = {}
_alerts_in_flight_lock = threading.Lock()


def _resolve_routeid(request: Request, routeid: str, response: Response | None = None) -> str:
    """Map a routeid absorbed by a direction merge onto the surviving route.

    Unknown ids pass through untouched so that a genuine typo still 404s rather
    than silently resolving to something else. Responses echo the canonical id,
    which lets clients that persist it heal themselves without a new release.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return routeid

    canonical = get_route_alias_index(settings).canonical(routeid)
    if canonical != routeid:
        LOGGER.info("resolved alias routeid=%s canonical=%s", routeid, canonical)
        if response is not None:
            response.headers["X-Route-Alias-Resolved"] = routeid
    return canonical


def _encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    factor = 10**precision
    output: list[str] = []
    previous_lat = 0
    previous_lon = 0

    for lat, lon in points:
        lat_value = int(round(lat * factor))
        lon_value = int(round(lon * factor))
        for value in (lat_value - previous_lat, lon_value - previous_lon):
            encoded = value << 1
            if value < 0:
                encoded = ~encoded
            while encoded >= 0x20:
                output.append(chr((0x20 | (encoded & 0x1F)) + 63))
                encoded >>= 5
            output.append(chr(encoded + 63))
        previous_lat = lat_value
        previous_lon = lon_value

    return "".join(output)


def _load_stop_geometry_points(
    static_route: dict | None,
    pathid: int,
) -> list[tuple[float, float]]:
    if static_route is None:
        return []

    path = static_route.get("paths", {}).get(pathid)
    if path is None:
        return []

    points: list[tuple[float, float]] = []
    previous: tuple[float, float] | None = None
    for stop in path.get("stops", []):
        try:
            lat = float(stop["lat"])
            lon = float(stop["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        point = (lat, lon)
        if point == previous:
            continue
        points.append(point)
        previous = point

    return points


_city_name_to_prefix_lower = {
    city.lower(): prefix for city, prefix in CITY_NAME_TO_PREFIX.items()
}
_city_name_canonical_lower = {city.lower(): city for city in CITY_NAME_TO_PREFIX}


def _resolve_city(city: str) -> tuple[str, str] | None:
    normalized = city.strip()
    if not normalized:
        return None

    if len(normalized) == 3 and normalized.isalpha():
        prefix = normalized.upper()
        city_name = CITY_PREFIX_TO_NAME.get(prefix)
        if city_name is None:
            return None
        return prefix, city_name

    city_name = _city_name_canonical_lower.get(normalized.lower())
    if city_name is None:
        return None
    prefix = _city_name_to_prefix_lower.get(city_name.lower())
    if prefix is None:
        return None
    return prefix, city_name


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius = 6378137.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius * c


def _search_routes(
    connection,
    *,
    routeid_like: str | None,
    query: str,
    limit: int,
) -> list[dict]:
    normalized_query = query.strip()
    prefix_clause = ""
    query_args: dict[str, object] = {"limit": limit}
    if routeid_like:
        prefix_clause = "AND routes.routeid LIKE :routeid_like"
        query_args["routeid_like"] = routeid_like

    where_clause = ""
    if normalized_query:
        where_clause = """
            AND (
                routes.name LIKE :wildcard
                OR routes.name_en LIKE :wildcard
                OR routes.routeid LIKE :wildcard
                OR EXISTS (
                    SELECT 1
                    FROM paths p
                    WHERE p.routeid = routes.routeid
                      AND (p.name LIKE :wildcard OR p.name_en LIKE :wildcard)
                )
            )
        """
        wildcard = f"%{normalized_query}%"
        query_args.update(
            {
                "query_lower": normalized_query.lower(),
                "prefix": f"{normalized_query.lower()}%",
                "wildcard": wildcard,
                "query_length": len(normalized_query),
            }
        )

    order_clause = "ORDER BY routes.routeid ASC"
    if normalized_query:
        order_clause = """
        ORDER BY
            CASE
                WHEN LOWER(TRIM(COALESCE(routes.name, ''))) = :query_lower
                  OR LOWER(TRIM(COALESCE(routes.name_en, ''))) = :query_lower THEN 0
                WHEN LOWER(TRIM(COALESCE(routes.name, ''))) LIKE :prefix
                  OR LOWER(TRIM(COALESCE(routes.name_en, ''))) LIKE :prefix THEN 1
                WHEN LOWER(TRIM(COALESCE(routes.name, ''))) LIKE :wildcard
                  OR LOWER(TRIM(COALESCE(routes.name_en, ''))) LIKE :wildcard THEN 2
                WHEN LOWER(TRIM(COALESCE(routes.routeid, ''))) = :query_lower THEN 3
                WHEN LOWER(TRIM(COALESCE(routes.routeid, ''))) LIKE :wildcard THEN 4
                WHEN EXISTS (
                    SELECT 1 FROM paths p
                    WHERE p.routeid = routes.routeid
                      AND (
                          LOWER(TRIM(COALESCE(p.name, ''))) = :query_lower
                          OR LOWER(TRIM(COALESCE(p.name_en, ''))) = :query_lower
                      )
                ) THEN 5
                WHEN EXISTS (
                    SELECT 1 FROM paths p
                    WHERE p.routeid = routes.routeid
                      AND (
                          LOWER(TRIM(COALESCE(p.name, ''))) LIKE :prefix
                          OR LOWER(TRIM(COALESCE(p.name_en, ''))) LIKE :prefix
                      )
                ) THEN 6
                ELSE 7
            END ASC,
            MIN(
                ABS(LENGTH(TRIM(COALESCE(routes.name, ''))) - :query_length),
                ABS(LENGTH(TRIM(COALESCE(routes.name_en, ''))) - :query_length)
            ) ASC,
            LENGTH(TRIM(COALESCE(routes.name, ''))) ASC,
            LOWER(TRIM(COALESCE(routes.name, ''))) ASC,
            routes.routeid ASC
        """

    rows = connection.execute(
        f"""
        SELECT
            routes.routeid AS routeid,
            routes.name AS route_name,
            routes.name_en AS route_name_en,
            COALESCE(
                (
                    SELECT GROUP_CONCAT(path_name, ' / ')
                    FROM (
                        SELECT DISTINCT p.name AS path_name
                        FROM paths p
                        WHERE p.routeid = routes.routeid
                          AND TRIM(COALESCE(p.name, '')) <> ''
                        ORDER BY p.pathid ASC
                    )
                ),
                ''
            ) AS path_name,
            COALESCE(
                (
                    SELECT GROUP_CONCAT(path_name_en, ' / ')
                    FROM (
                        SELECT DISTINCT p.name_en AS path_name_en
                        FROM paths p
                        WHERE p.routeid = routes.routeid
                          AND TRIM(COALESCE(p.name_en, '')) <> ''
                        ORDER BY p.pathid ASC
                    )
                ),
                ''
            ) AS path_name_en,
            COALESCE(
                (
                    SELECT p.pathid
                    FROM paths p
                    WHERE p.routeid = routes.routeid
                    ORDER BY p.pathid ASC
                    LIMIT 1
                ),
                0
            ) AS pathid,
            SUBSTR(routes.routeid, 1, 3) AS city_code
        FROM routes
        WHERE 1 = 1
        {prefix_clause}
        {where_clause}
        {order_clause}
        LIMIT :limit
        """,
        query_args,
    ).fetchall()

    return [
        {
            "routeid": row["routeid"],
            "route_name": row["route_name"],
            "route_name_en": row["route_name_en"],
            "pathid": int(row["pathid"]),
            "path_name": row["path_name"],
            "path_name_en": row["path_name_en"],
            "city_code": row["city_code"],
        }
        for row in rows
    ]


def _load_nearby_stops(
    connection,
    *,
    routeid_like: str,
    latitude: float,
    longitude: float,
    radius: float,
    limit: int,
) -> list[dict]:
    lat_delta = radius / 111320
    lon_scale = abs(math.cos(math.radians(latitude)))
    lon_delta = 180.0 if lon_scale < 1e-6 else radius / (111320 * lon_scale)
    candidate_limit = max(limit * 25, 200)

    rows = connection.execute(
        """
        SELECT
            s.routeid AS routeid,
            s.pathid AS pathid,
            s.stopid AS stopid,
            s.name AS stop_name,
            s.name_en AS stop_name_en,
            s.seq AS seq,
            s.lat AS lat,
            s.lon AS lon,
            r.name AS route_name,
            r.name_en AS route_name_en,
            COALESCE(p.name, '') AS path_name,
            COALESCE(p.name_en, '') AS path_name_en,
            SUBSTR(s.routeid, 1, 3) AS city_code
        FROM stops s
        JOIN routes r ON r.routeid = s.routeid
        LEFT JOIN paths p ON p.routeid = s.routeid AND p.pathid = s.pathid
        WHERE s.routeid LIKE ?
          AND s.lat BETWEEN ? AND ?
          AND s.lon BETWEEN ? AND ?
        ORDER BY s.routeid ASC, s.pathid ASC, s.seq ASC
        LIMIT ?
        """,
        (
            routeid_like,
            latitude - lat_delta,
            latitude + lat_delta,
            longitude - lon_delta,
            longitude + lon_delta,
            candidate_limit,
        ),
    ).fetchall()

    results: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for row in rows:
        row_lat = float(row["lat"])
        row_lon = float(row["lon"])
        distance = _distance_meters(latitude, longitude, row_lat, row_lon)
        if distance > radius:
            continue

        dedupe_key = (row["routeid"], int(row["pathid"]), row["stopid"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        results.append(
            {
                "routeid": row["routeid"],
                "pathid": int(row["pathid"]),
                "stopid": row["stopid"],
                "stop_name": row["stop_name"],
                "stop_name_en": row["stop_name_en"],
                "seq": int(row["seq"]),
                "lat": row_lat,
                "lon": row_lon,
                "distance": round(distance, 2),
                "route_name": row["route_name"],
                "route_name_en": row["route_name_en"],
                "path_name": row["path_name"],
                "path_name_en": row["path_name_en"],
                "city_code": row["city_code"],
            }
        )

    results.sort(key=lambda item: item["distance"])
    return results[:limit]


@router.get("/", include_in_schema=False)
def root() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html", media_type="text/html")


@router.get("/brand/icon.png", include_in_schema=False)
def brand_icon() -> FileResponse:
    return FileResponse(WEB_ROOT / "static" / "icon_transparent.png", media_type="image/png")


@router.get("/downloads/bus.db")
def download_bus_db(request: Request) -> FileResponse:
    db_path = request.app.state.settings.download_db_path
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="bus.db does not exist yet.")
    return FileResponse(db_path, filename="bus.db")


@router.get("/downloads/{name}.db")
def download_city_db(name: str, request: Request) -> FileResponse:
    if not _download_name_pattern.fullmatch(name):
        raise HTTPException(status_code=404, detail="Invalid download database name.")

    db_path = request.app.state.settings.download_db_path.parent / f"{name}.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"{name}.db does not exist yet.")
    return FileResponse(db_path, filename=f"{name}.db")


_BATCH_ROUTES_REALTIME_MAX_IDS = 25
_batch_routeid_pattern = re.compile(r"^[A-Za-z]{3}[A-Za-z0-9_-]+$")


@router.get("/api/v1/batchroutes/{routeids}/realtime")
def get_batch_routes_realtime(routeids: str, request: Request) -> dict:
    """Return realtime snapshots for up to 25 comma-separated route IDs.

    The server groups routes by city and makes one TDX batch request per
    city, which is far more efficient than the client calling the single-
    route endpoint 25 times and avoids 429 errors.
    """
    parts = [p.strip() for p in routeids.split(",") if p.strip()]
    if not parts:
        raise HTTPException(status_code=400, detail="No route IDs provided.")
    if len(parts) > _BATCH_ROUTES_REALTIME_MAX_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many route IDs (max {_BATCH_ROUTES_REALTIME_MAX_IDS}, got {len(parts)}).",
        )

    # Validate that each part looks like a plausible route ID.
    for part in parts:
        if not _batch_routeid_pattern.fullmatch(part):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid route ID: {part!r}",
            )

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique_parts: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique_parts.append(part)

    service = request.app.state.realtime_service
    try:
        batch = service.get_batch_snapshots(unique_parts)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc

    return {"routes": batch}


@router.get("/api/v1/routes/{routeid}/realtime")
def get_route_realtime(routeid: str, request: Request) -> dict:
    service = request.app.state.realtime_service
    try:
        return service.get_snapshot(routeid)
    except RouteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc


@router.get("/api/v1/cities/{city}/routes")
def search_city_routes(
    city: str,
    request: Request,
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=80, ge=1, le=200),
) -> list[dict]:
    resolved_city = _resolve_city(city)
    if resolved_city is None:
        raise HTTPException(status_code=404, detail=f"City {city} was not found.")
    prefix, city_name = resolved_city

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        return _search_routes(
            connection,
            routeid_like=f"{prefix}%",
            query=query,
            limit=limit,
        )


@router.get("/api/v1/routes")
def search_routes(
    request: Request,
    query: str = Query(default="", max_length=120),
    limit: int = Query(default=120, ge=1, le=300),
) -> list[dict]:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        return _search_routes(
            connection,
            routeid_like=None,
            query=query,
            limit=limit,
        )


@router.get("/api/v1/cities/{city}/stops/nearby")
def get_city_nearby_stops(
    city: str,
    request: Request,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius: float = Query(default=500, gt=0, le=3000),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[dict]:
    resolved_city = _resolve_city(city)
    if resolved_city is None:
        raise HTTPException(status_code=404, detail=f"City {city} was not found.")
    prefix, _ = resolved_city

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        return _load_nearby_stops(
            connection,
            routeid_like=f"{prefix}%",
            latitude=lat,
            longitude=lon,
            radius=radius,
            limit=limit,
        )


@router.get("/api/v1/routes/{routeid}/realtime/buses")
def get_route_buses(routeid: str, request: Request) -> list[dict]:
    service = request.app.state.route_buses_service
    try:
        return service.get_buses(routeid)
    except RouteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc


@router.get("/api/v1/routes/{routeid}/paths/{pathid}/points")
def get_route_path_points(routeid: str, pathid: int, request: Request, response: Response) -> dict:
    settings = request.app.state.settings
    # pathid needs no remapping: the merge preserves pathid == Direction.
    routeid = _resolve_routeid(request, routeid, response)
    with get_connection(settings.db_path) as connection:
        if not path_exists(connection, routeid, pathid):
            raise HTTPException(
                status_code=404,
                detail=f"Path {pathid} for route {routeid} was not found.",
            )
        points = load_path_points(connection, routeid, pathid)
        if len(points) < 2:
            stop_points = _load_stop_geometry_points(
                load_route_static(connection, routeid),
                pathid,
            )
            if len(stop_points) >= 2:
                points = stop_points

    return {
        "routeid": routeid,
        "pathid": pathid,
        "polyline": _encode_polyline(points),
    }


_STOP_PASSBY_MAX_ROUTES = 200
_stop_passby_id_pattern = re.compile(r"^[A-Za-z0-9_-]+$")


@router.get("/api/v1/stations/resolve")
def resolve_bus_station(
    request: Request,
    city: str = Query(..., min_length=1, max_length=8),
    stopid: str = Query(..., min_length=1, max_length=128),
) -> dict:
    """Resolve an authority-scoped raw StopID/StopUID to a physical station."""
    city_code = _normalize_station_city(city)
    normalized_stopid = stopid.strip()
    if not _stop_passby_id_pattern.fullmatch(normalized_stopid):
        raise HTTPException(status_code=400, detail=f"Invalid stop ID: {stopid!r}")

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        row = connection.execute(
            """
            SELECT station_id
            FROM station_stops
            WHERE city_code = ? AND (stop_id = ? OR stop_uid = ?)
            ORDER BY station_id
            LIMIT 1
            """,
            (city_code, normalized_stopid, normalized_stopid),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Stop {stopid} was not resolved to a station.",
        )
    return _station_passby_payload(
        request,
        city_code=city_code,
        station_id=row["station_id"],
    )


@router.get("/api/v1/stations/{station_id}/passby")
def get_station_passby(
    station_id: str,
    request: Request,
    city: str = Query(..., min_length=1, max_length=8),
) -> dict:
    """Return every stable side of a physical station and its passing routes."""
    normalized_station_id = station_id.strip()
    if not normalized_station_id or not _stop_passby_id_pattern.fullmatch(
        normalized_station_id
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid station ID: {station_id!r}",
        )
    return _station_passby_payload(
        request,
        city_code=_normalize_station_city(city),
        station_id=normalized_station_id,
    )


def _normalize_station_city(city: str) -> str:
    city_code = city.strip().upper()
    if city_code not in CITY_PREFIX_TO_NAME:
        raise HTTPException(status_code=400, detail=f"Unknown city prefix: {city!r}")
    return city_code


# Sides closer together than this are the same physical pole. TDX mints a
# separate StopUID per route family at one stop, so a single kerbside pole
# routinely arrives as several "sides": 豐樂公園 carries 99/99延, 綠3, 73, 綠1
# and 365 on five UIDs at one position. Grouping them restores what a passenger
# actually sees standing there. Measured against production, 0 m already merges
# 70% of multi-side stations and 10 m reaches 73%; beyond that the gain flattens
# and the risk of swallowing the far kerb starts to rise.
_STATION_SIDE_MERGE_METERS = 10.0


def _rough_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Equirectangular metres — plenty accurate at the scale of one bus stop.

    Matches the flat-earth constant the nearby-stops bounding box above uses.
    """
    lat_metres = (lat2 - lat1) * 111320
    lon_metres = (lon2 - lon1) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(lat_metres, lon_metres)


def _station_side_label(index: int) -> str:
    """Spreadsheet-style labels: A..Z, AA..AZ, and so on.

    Merged sides are relabelled from scratch so the letters stay contiguous;
    the stored side_label describes the pre-merge rows and would otherwise show
    gaps like "A, F".
    """
    value = index + 1
    label = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def _cluster_station_sides(side_rows: Collection) -> list[list]:
    """Group side rows sitting on the same pole, preserving side_order."""
    clusters: list[list] = []
    for row in side_rows:
        lat, lon = float(row["lat"]), float(row["lon"])
        for cluster in clusters:
            head = cluster[0]
            if (
                _rough_distance_meters(
                    lat, lon, float(head["lat"]), float(head["lon"])
                )
                <= _STATION_SIDE_MERGE_METERS
            ):
                cluster.append(row)
                break
        else:
            clusters.append([row])
    return clusters


def _station_passby_payload(
    request: Request,
    *,
    city_code: str,
    station_id: str,
) -> dict:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        station = connection.execute(
            """
            SELECT station_id, name, name_en, lat, lon
            FROM stations
            WHERE city_code = ? AND station_id = ?
            """,
            (city_code, station_id),
        ).fetchone()
        if station is None:
            raise HTTPException(
                status_code=404,
                detail=f"Station {station_id} was not found.",
            )

        side_rows = connection.execute(
            """
            SELECT side_id, side_order, side_label, stop_uid, stop_id,
                   direction, lat, lon
            FROM station_stops
            WHERE city_code = ? AND station_id = ?
            ORDER BY side_order, side_id
            """,
            (city_code, station_id),
        ).fetchall()
        route_rows = connection.execute(
            """
            SELECT
                ss.side_id AS side_id,
                s.routeid AS routeid,
                s.pathid AS pathid,
                s.seq AS seq,
                s.stopid AS stopid,
                r.name AS route_name,
                r.name_en AS route_name_en,
                COALESCE(p.name, '') AS path_name,
                COALESCE(p.name_en, '') AS path_name_en
            FROM station_stops ss
            JOIN stops s
              ON s.stopid = ss.stop_id
             AND s.routeid LIKE ?
            JOIN routes r ON r.routeid = s.routeid
            LEFT JOIN paths p
              ON p.routeid = s.routeid AND p.pathid = s.pathid
            WHERE ss.city_code = ? AND ss.station_id = ?
            ORDER BY ss.side_order, s.routeid, s.pathid, s.seq
            """,
            (f"{city_code}%", city_code, station_id),
        ).fetchall()

    ordered_routeids: list[str] = []
    seen_routeids: set[str] = set()
    for row in route_rows:
        routeid = row["routeid"]
        if routeid not in seen_routeids:
            seen_routeids.add(routeid)
            ordered_routeids.append(routeid)
    ordered_routeids = ordered_routeids[:_STOP_PASSBY_MAX_ROUTES]
    allowed_routeids = set(ordered_routeids)

    service = request.app.state.realtime_service
    try:
        snapshots = service.get_batch_snapshots(ordered_routeids)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc

    routes_by_side: dict[str, list[dict]] = {
        row["side_id"]: [] for row in side_rows
    }
    for row in route_rows:
        routeid = row["routeid"]
        if routeid not in allowed_routeids:
            continue
        pathid = int(row["pathid"])
        stopid = row["stopid"]
        stop_bucket = _find_snapshot_stop(snapshots.get(routeid), pathid, stopid)
        routes_by_side[row["side_id"]].append(
            {
                "routeid": routeid,
                "route_name": row["route_name"],
                "route_name_en": row["route_name_en"],
                "pathid": pathid,
                "path_name": row["path_name"],
                "path_name_en": row["path_name_en"],
                "seq": int(row["seq"]),
                "stopid": stopid,
                "eta": (stop_bucket or {}).get("eta"),
                "message": (stop_bucket or {}).get("message", ""),
                "updated_at": (stop_bucket or {}).get("updated_at"),
                "buses": (stop_bucket or {}).get("buses", []),
                "etas": (stop_bucket or {}).get("etas", []),
            }
        )

    return {
        "city": city_code,
        "station_id": station["station_id"],
        "station_name": station["name"],
        "station_name_en": station["name_en"],
        "lat": float(station["lat"]),
        "lon": float(station["lon"]),
        "sides": [
            {
                # Representative values: the merged side spans several StopUIDs,
                # and every route below carries its own stopid, so nothing
                # downstream resolves anything through these.
                "side_id": cluster[0]["side_id"],
                "label": _station_side_label(index),
                "direction": next(
                    (row["direction"] for row in cluster if row["direction"]), None
                ),
                "stop_uid": cluster[0]["stop_uid"],
                "stopid": cluster[0]["stop_id"],
                "lat": float(cluster[0]["lat"]),
                "lon": float(cluster[0]["lon"]),
                "routes": [
                    route
                    for row in cluster
                    for route in routes_by_side.get(row["side_id"], [])
                ],
            }
            for index, cluster in enumerate(_cluster_station_sides(side_rows))
        ],
    }


@router.get("/api/v1/stops/{stopid}/passby")
def get_stop_passby(
    stopid: str,
    request: Request,
    city: str = Query(default="", max_length=8),
) -> dict:
    """Return the routes passing a stop, each with only that stop's realtime ETA.

    A single stop can be served by dozens of routes.  Rather than have the
    client request a full realtime snapshot for every one of those routes
    (each snapshot carries *all* the route's stops), this endpoint resolves
    which routes stop here, fetches their snapshots server-side (batched per
    city via the realtime service), and returns just the single stop bucket
    for each route.  The payload is therefore proportional to the number of
    passing routes, not to the total number of stops across them.

    TDX StopIDs are only unique within a single authority — Taichung's "39"
    is a different physical stop from Matsu's "39" — so ``city`` (a routeid
    prefix such as ``TXG`` or ``INT``) scopes the lookup to one authority.
    Omitting it keeps the legacy nationwide behaviour for older clients.
    """
    normalized_stopid = stopid.strip()
    if not normalized_stopid or not _stop_passby_id_pattern.fullmatch(normalized_stopid):
        raise HTTPException(status_code=400, detail=f"Invalid stop ID: {stopid!r}")

    routeid_prefix = city.strip().upper()
    if routeid_prefix and routeid_prefix not in CITY_PREFIX_TO_NAME:
        raise HTTPException(status_code=400, detail=f"Unknown city prefix: {city!r}")

    prefix_clause = "AND s.routeid LIKE ?" if routeid_prefix else ""
    query_args: tuple[str, ...] = (
        (normalized_stopid, f"{routeid_prefix}%")
        if routeid_prefix
        else (normalized_stopid,)
    )

    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        stop_rows = connection.execute(
            f"""
            SELECT
                s.routeid AS routeid,
                s.pathid AS pathid,
                s.seq AS seq,
                s.name AS stop_name,
                s.name_en AS stop_name_en,
                r.name AS route_name,
                r.name_en AS route_name_en,
                COALESCE(p.name, '') AS path_name,
                COALESCE(p.name_en, '') AS path_name_en
            FROM stops s
            JOIN routes r ON r.routeid = s.routeid
            LEFT JOIN paths p ON p.routeid = s.routeid AND p.pathid = s.pathid
            WHERE s.stopid = ? {prefix_clause}
            ORDER BY s.routeid ASC, s.pathid ASC
            """,
            query_args,
        ).fetchall()

    if not stop_rows:
        raise HTTPException(status_code=404, detail=f"Stop {stopid} was not found.")

    # Preserve the DB order while deduplicating route IDs for the batch fetch.
    seen_routeids: set[str] = set()
    ordered_routeids: list[str] = []
    for row in stop_rows:
        routeid = row["routeid"]
        if routeid not in seen_routeids:
            seen_routeids.add(routeid)
            ordered_routeids.append(routeid)

    if len(ordered_routeids) > _STOP_PASSBY_MAX_ROUTES:
        ordered_routeids = ordered_routeids[:_STOP_PASSBY_MAX_ROUTES]

    stop_name = next(
        (row["stop_name"] for row in stop_rows if row["stop_name"]),
        "",
    )
    stop_name_en = next(
        (row["stop_name_en"] for row in stop_rows if row["stop_name_en"]),
        None,
    )

    service = request.app.state.realtime_service
    try:
        snapshots = service.get_batch_snapshots(ordered_routeids)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc

    routes_out: list[dict] = []
    for row in stop_rows:
        routeid = row["routeid"]
        if routeid not in seen_routeids:
            continue
        pathid = int(row["pathid"])
        snapshot = snapshots.get(routeid)
        stop_bucket = _find_snapshot_stop(snapshot, pathid, normalized_stopid)

        routes_out.append(
            {
                "routeid": routeid,
                "route_name": row["route_name"],
                "route_name_en": row["route_name_en"],
                "pathid": pathid,
                "path_name": row["path_name"],
                "path_name_en": row["path_name_en"],
                "seq": int(row["seq"]),
                "stopid": normalized_stopid,
                "eta": (stop_bucket or {}).get("eta"),
                "message": (stop_bucket or {}).get("message", ""),
                "updated_at": (stop_bucket or {}).get("updated_at"),
                "buses": (stop_bucket or {}).get("buses", []),
                "etas": (stop_bucket or {}).get("etas", []),
            }
        )

    return {
        "stopid": normalized_stopid,
        "stop_name": stop_name,
        "stop_name_en": stop_name_en,
        "routes": routes_out,
    }


def _find_snapshot_stop(
    snapshot: dict | None,
    pathid: int,
    stopid: str,
) -> dict | None:
    if not snapshot:
        return None
    for path in snapshot.get("paths") or []:
        if not isinstance(path, dict) or path.get("pathid") != pathid:
            continue
        for stop_bucket in path.get("stops") or []:
            if isinstance(stop_bucket, dict) and stop_bucket.get("stopid") == stopid:
                return stop_bucket
    return None


@router.get("/api/v1/routes/{routeid}/stops")
def get_route_stops(routeid: str, request: Request, response: Response) -> dict:
    settings = request.app.state.settings
    routeid = _resolve_routeid(request, routeid, response)
    with get_connection(settings.db_path) as connection:
        static_route = load_route_static(connection, routeid)
    if static_route is None:
        raise HTTPException(status_code=404, detail=f"Route {routeid} was not found.")

    response_paths = []
    for pathid in sorted(static_route["paths"]):
        path = static_route["paths"][pathid]
        response_paths.append(
            {
                "pathid": pathid,
                "name": path["name"],
                "name_en": path["name_en"],
                "stops": list(path["stops"]),
            }
        )

    return {
        "routeid": routeid,
        "name": static_route["name"],
        "name_en": static_route["name_en"],
        "paths": response_paths,
    }


def _parse_tdx_datetime_to_unix(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _extract_stop_ids(alert: dict) -> list[str]:
    stop_ids: list[str] = []
    scopes = alert.get("Scope") or alert.get("AlertScopes")
    if not scopes:
        return stop_ids
    if isinstance(scopes, dict):
        scopes = [scopes]
    if not isinstance(scopes, list):
        return stop_ids
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        stops = scope.get("Stops") or []
        for stop in stops:
            if not isinstance(stop, dict):
                continue
            stop_id = stop.get("StopID") or stop.get("StopUID") or ""
            if stop_id:
                stop_ids.append(str(stop_id))
    return stop_ids


def _alert_matches_route(alert: dict, routeids: Collection[str]) -> bool:
    """Match an alert against every SubRouteUID a (possibly merged) route covers.

    Matching a single id is not enough once directions are merged: an alert
    scoped to the return direction's SubRouteUID belongs to the surviving route
    just as much as one scoped to the outbound id.
    """
    route_id = alert.get("RouteID") or ""
    route_uid = alert.get("RouteUID") or ""
    if route_id in routeids or route_uid in routeids:
        return True

    scopes = alert.get("Scope") or alert.get("AlertScopes")
    if not scopes:
        return False
    if isinstance(scopes, dict):
        scopes = [scopes]
    if not isinstance(scopes, list):
        return False
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        for route in scope.get("Routes") or []:
            if isinstance(route, dict):
                if route.get("RouteID", "") in routeids or route.get("RouteUID", "") in routeids:
                    return True
        for subroute in scope.get("SubRoutes") or []:
            if isinstance(subroute, dict):
                sub_uid = subroute.get("SubRouteUID") or subroute.get("SubRouteID") or ""
                if any(sub_uid.startswith(routeid) for routeid in routeids if routeid):
                    return True
    return False


def _format_alert(alert: dict) -> dict:
    title_obj = alert.get("Title") or ""
    desc_obj = alert.get("Description") or ""
    scope_obj = alert.get("Scope") or ""
    if isinstance(title_obj, dict):
        title_obj = title_obj.get("Zh_tw") or title_obj.get("En") or ""
    if isinstance(desc_obj, dict):
        desc_obj = desc_obj.get("Zh_tw") or desc_obj.get("En") or ""
    if isinstance(scope_obj, dict):
        scope_obj = scope_obj.get("Zh_tw") or scope_obj.get("En") or ""
    direction = alert.get("Direction")
    if direction is None:
        scopes = alert.get("Scope") or alert.get("AlertScopes")
        if isinstance(scopes, dict):
            scopes = [scopes]
        if isinstance(scopes, list):
            for sc in scopes:
                if isinstance(sc, dict):
                    for sr in sc.get("SubRoutes") or []:
                        if isinstance(sr, dict) and sr.get("Direction") is not None:
                            direction = sr["Direction"]
                            break
                if direction is not None:
                    break
    return {
        "alert_id": alert.get("AlertID") or "",
        "title": str(title_obj),
        "description": str(desc_obj),
        "status": alert.get("Status"),
        "cause": alert.get("Cause"),
        "effect": alert.get("Effect"),
        "direction": direction,
        "scope": str(scope_obj) if scope_obj else None,
        "stop_ids": _extract_stop_ids(alert),
        "start_time": _parse_tdx_datetime_to_unix(alert.get("StartTime")),
        "end_time": _parse_tdx_datetime_to_unix(alert.get("EndTime")),
        "publish_time": _parse_tdx_datetime_to_unix(alert.get("PublishTime")),
        "updated_time": _parse_tdx_datetime_to_unix(alert.get("UpdateTime")),
    }


def _fetch_city_alerts_cached(request: Request, city_name: str) -> list[dict]:
    with _alerts_cache_lock:
        cached = _alerts_cache.get(city_name)
        if cached is not None and cached.is_fresh:
            return cached.alerts

    # Prevent thundering herd: only one thread fetches per city.
    with _alerts_in_flight_lock:
        # Double-check cache after acquiring lock.
        with _alerts_cache_lock:
            cached = _alerts_cache.get(city_name)
            if cached is not None and cached.is_fresh:
                return cached.alerts

    try:
        tdx_client = request.app.state.tdx_client
        raw_alerts = tdx_client.fetch_alerts(city_name)
        with _alerts_cache_lock:
            _alerts_cache[city_name] = _AlertsCacheEntry(alerts=raw_alerts)
        return raw_alerts
    except Exception as exc:
        LOGGER.warning("Failed to fetch alerts for %s: %s", city_name, exc)
        # Return stale cache if available.
        with _alerts_cache_lock:
            stale = _alerts_cache.get(city_name)
            if stale is not None:
                return stale.alerts
        raise


@router.get("/api/v1/routes/{routeuid}/alerts")
def get_route_alerts(routeuid: str, request: Request, response: Response) -> dict:
    canonical = _resolve_routeid(request, routeuid, response)
    prefix = canonical[:3].upper()
    city_name = CITY_PREFIX_TO_NAME.get(prefix)
    if city_name is None:
        raise HTTPException(status_code=404, detail=f"Unknown city prefix for route {routeuid}.")

    alias_index = get_route_alias_index(request.app.state.settings)
    routeids = {member[3:] for member in alias_index.subroute_uids(canonical)}

    try:
        city_alerts = _fetch_city_alerts_cached(request, city_name)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="TDX upstream request failed.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    matched = [
        _format_alert(alert)
        for alert in city_alerts
        if _alert_matches_route(alert, routeids)
    ]
    return {"routeid": canonical, "alerts": matched}


@router.get("/api/v1/database/{name}/version")
def get_database_version(name: str, request: Request) -> dict:
    settings = request.app.state.settings
    with get_connection(settings.db_path) as connection:
        version = load_database_version(connection, name)
    if version is None:
        raise HTTPException(status_code=404, detail=f"Database version for {name} was not found.")
    return version


@router.get("/api/v1/routes/{routeid}/operators")
def get_route_operators(routeid: str, request: Request, response: Response) -> list[dict]:
    settings = request.app.state.settings
    routeid = _resolve_routeid(request, routeid, response)
    with get_connection(settings.db_path) as connection:
        rows = connection.execute(
            """
            SELECT o.operator_id, o.name, o.name_en, o.code, o.phone, o.email, o.url
            FROM route_operators ro
            JOIN operators o ON o.operator_id = ro.operator_id
            WHERE ro.routeid = ?
            ORDER BY ro.seq
            """,
            (routeid,),
        ).fetchall()
    return [
        {
            "operator_id": row["operator_id"],
            "name": row["name"],
            "name_en": row["name_en"],
            "code": row["code"],
            "phone": row["phone"],
            "email": row["email"],
            "url": row["url"],
        }
        for row in rows
    ]


@router.get("/api/v1/routes/{routeid}/schedule")
def get_route_schedule(routeid: str, request: Request, response: Response) -> list[dict]:
    settings = request.app.state.settings
    routeid = _resolve_routeid(request, routeid, response)
    with get_connection(settings.db_path) as connection:
        rows = connection.execute(
            """
            SELECT subroute_uid, direction, kind, seq, service_days, payload
            FROM route_schedules
            WHERE routeid = ?
            ORDER BY subroute_uid, direction, kind, seq
            """,
            (routeid,),
        ).fetchall()
    import json as _json
    return [
        {
            "subroute_uid": row["subroute_uid"],
            "direction": row["direction"],
            "kind": row["kind"],
            "seq": row["seq"],
            "service_days": _json.loads(row["service_days"]),
            "payload": _json.loads(row["payload"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Stop estimated times (frequency trips extrapolated via travel-time data)
# ---------------------------------------------------------------------------

def _estimate_stop_times_for_frequency(
    connection,
    routeid: str,
    direction: int,
    freq_payload: dict,
) -> list[dict] | None:
    """Attempts to build estimated per-stop times for a frequency entry.

    Uses the *stop_travel_times* table to compute cumulative travel time
    from the first stop.  ETA-sourced travel times (``source = 'eta'``)
    are preferred over timetable-derived ones because they reflect
    real-world conditions.  Returns a list of ``{seq, stopid, arrival,
    departure, estimated}`` dicts, or *None* if the route has insufficient
    travel-time data (e.g.  a route that never had any timetable entries
    and has not yet been observed with live buses).

    The *freq_payload* dict must contain ``start`` (HH:MM first-bus time).
    """
    import json as _json

    # 1. Fetch ordered stops for this route+direction.
    stops_rows = connection.execute(
        """
        SELECT seq, stopid
        FROM stops
        WHERE routeid = ? AND pathid = ?
        ORDER BY seq
        """,
        (routeid, direction),
    ).fetchall()
    if len(stops_rows) < 2:
        return None

    # 2. Fetch travel-time segments, preferring ETA-sourced data.
    # For each (from_seq, to_seq) pair, if an 'eta' row exists use it;
    # otherwise fall back to the 'timetable' row.
    tt_rows = connection.execute(
        """
        SELECT from_seq, to_seq, avg_seconds, source
        FROM stop_travel_times
        WHERE routeid = ? AND direction = ?
        """,
        (routeid, direction),
    ).fetchall()
    if not tt_rows:
        return None

    # Build seg_map: prefer 'eta' over 'timetable' for the same segment.
    seg_map: dict[tuple[int, int], float] = {}
    seg_source: dict[tuple[int, int], str] = {}
    for r in tt_rows:
        key = (r["from_seq"], r["to_seq"])
        source = r["source"]
        # Prefer eta over timetable.
        if key not in seg_map or source == "eta":
            seg_map[key] = r["avg_seconds"]
            seg_source[key] = source

    # 3. Walk the stop list and accumulate travel time from the first stop.
    start_str = (freq_payload.get("start") or "").strip()
    if not start_str:
        return None
    base_minutes = _time_str_to_minutes_api(start_str)
    if base_minutes is None:
        return None

    has_eta_source = any(s == "eta" for s in seg_source.values())

    result: list[dict] = []
    cumulative_seconds = 0.0
    prev_seq: int | None = None
    covered = True

    for stop in stops_rows:
        seq = stop["seq"]
        if prev_seq is not None:
            seg = seg_map.get((prev_seq, seq))
            if seg is not None:
                cumulative_seconds += seg
            else:
                covered = False

        total_minutes = base_minutes + cumulative_seconds / 60.0
        # Handle cross-midnight wrap
        if total_minutes >= 1440:
            total_minutes -= 1440
        hh = int(total_minutes) // 60
        mm = int(total_minutes) % 60
        time_str = f"{hh:02d}:{mm:02d}"

        result.append({
            "seq": seq,
            "stopid": stop["stopid"],
            "arrival": time_str,
            "departure": time_str,
            "estimated": True,
        })
        prev_seq = seq

    # If any gaps existed, still return partial results – each entry has
    # `estimated: True` so the client can display a caveat.
    return result if result else None


def _time_str_to_minutes_api(time_str: str) -> float | None:
    """Converts 'HH:MM' to minutes since midnight."""
    if not time_str:
        return None
    parts = time_str.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60.0 + int(parts[1])
    except (ValueError, IndexError):
        return None


@router.get("/api/v1/routes/{routeid}/stop-estimated-times")
def get_stop_estimated_times(routeid: str, request: Request, response: Response) -> dict:
    """Returns per-stop estimated arrival/departure times for a route.

    For timetable entries the actual times from the TDX StopTimes are
    returned (with ``estimated: false``).  For frequency entries the API
    attempts to extrapolate per-stop times using the *stop_travel_times*
    table; if that fails (no travel-time data) the frequency entry keeps
    its original payload (no per-stop times).

    The response format mirrors the schedule endpoint but adds an
    ``estimated`` boolean to each stop_time entry.
    """
    settings = request.app.state.settings
    routeid = _resolve_routeid(request, routeid, response)
    with get_connection(settings.db_path) as connection:
        rows = connection.execute(
            """
            SELECT subroute_uid, direction, kind, seq, service_days, payload
            FROM route_schedules
            WHERE routeid = ?
            ORDER BY subroute_uid, direction, kind, seq
            """,
            (routeid,),
        ).fetchall()

    import json as _json
    entries: list[dict] = []
    for row in rows:
        payload = _json.loads(row["payload"])
        kind = row["kind"]

        if kind == "timetable":
            # Mark existing stop_times as authoritative (not estimated).
            stop_times = payload.get("stop_times") or []
            for st in stop_times:
                st["estimated"] = False
            entries.append({
                "subroute_uid": row["subroute_uid"],
                "direction": row["direction"],
                "kind": kind,
                "seq": row["seq"],
                "service_days": _json.loads(row["service_days"]),
                "payload": payload,
            })
        elif kind == "frequency":
            # Try to extrapolate per-stop times.
            direction = row["direction"]
            with get_connection(settings.db_path) as conn:
                estimated_stops = _estimate_stop_times_for_frequency(
                    conn, routeid, direction, payload,
                )
            if estimated_stops is not None:
                payload["stop_times"] = estimated_stops
                payload["has_estimated_stops"] = True
            entries.append({
                "subroute_uid": row["subroute_uid"],
                "direction": row["direction"],
                "kind": kind,
                "seq": row["seq"],
                "service_days": _json.loads(row["service_days"]),
                "payload": payload,
            })

    return {"routeid": routeid, "entries": entries}


# ---------------------------------------------------------------------------
# Taiwan holiday calendar
# ---------------------------------------------------------------------------

from pathlib import Path as _Path

# Source: ruyut/TaiwanCalendar open data (updated every year, includes
# make-up working days / 補班 and adjusted long weekends). Served via jsDelivr
# CDN for reliability. Each year is a list of daily objects:
#   {"date": "YYYYMMDD", "week": "一", "isHoliday": bool, "description": str}
_TAIWAN_CALENDAR_URL = (
    "https://cdn.jsdelivr.net/gh/ruyut/TaiwanCalendar/data/{year}.json"
)
_TAIWAN_CALENDAR_FALLBACK_URL = (
    "https://raw.githubusercontent.com/ruyut/TaiwanCalendar/master/data/{year}.json"
)
_HOLIDAYS_LOCAL_FILE = _Path(__file__).resolve().parent.parent / "data" / "holidays.json"

HOLIDAYS_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


@dataclass
class _HolidayCacheEntry:
    # date string "YYYY-MM-DD" -> {"isHoliday": bool, "name": str | None}
    entries: dict[str, dict]
    fetched_at: float = field(default_factory=time.monotonic)

    @property
    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < HOLIDAYS_CACHE_TTL_SECONDS


_holidays_cache: dict[int, _HolidayCacheEntry] = {}
_holidays_lock = threading.Lock()


def _parse_taiwan_calendar(raw: list) -> dict[str, dict]:
    """Converts the TaiwanCalendar daily list into a date->info map.

    Only keeps days where ``isHoliday`` is True OR a make-up working day
    (a weekend date marked isHoliday=False), which is all the schedule logic
    needs. Plain weekdays follow the default weekend rule on the client side.
    """
    import json as _json

    result: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_date = str(item.get("date", "")).strip()
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        iso = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        try:
            parsed = datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            continue
        is_holiday = bool(item.get("isHoliday", False))
        is_weekend = parsed.weekday() >= 5
        # Keep holidays, and make-up working days (weekend but not a holiday).
        if is_holiday or (is_weekend and not is_holiday):
            name = item.get("description") or None
            result[iso] = {"isHoliday": is_holiday, "name": name}
    return result


def _fetch_taiwan_calendar(year: int) -> dict[str, dict] | None:
    """Fetches a year's calendar from the CDN (falling back to GitHub raw)."""
    for url_template in (_TAIWAN_CALENDAR_URL, _TAIWAN_CALENDAR_FALLBACK_URL):
        url = url_template.format(year=year)
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                continue
            return _parse_taiwan_calendar(response.json())
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.warning("failed to fetch Taiwan calendar %s from %s: %s", year, url, exc)
    return None


def _load_local_holidays() -> dict[str, dict[str, dict]]:
    """Loads the bundled fallback holiday file (year -> date -> info)."""
    import json as _json

    try:
        with _HOLIDAYS_LOCAL_FILE.open("r", encoding="utf-8") as handle:
            data = _json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("failed to load local holidays.json: %s", exc)
        return {}

    out: dict[str, dict[str, dict]] = {}
    for year_key, items in (data.get("holidays") or {}).items():
        year_map: dict[str, dict] = {}
        for item in items:
            if isinstance(item, dict) and item.get("date"):
                year_map[item["date"]] = {
                    "isHoliday": bool(item.get("isHoliday", True)),
                    "name": item.get("name"),
                }
        out[str(year_key)] = year_map
    return out


def _get_holidays_for_year(year: int) -> dict[str, dict]:
    """Returns a cached date->info map for ``year``.

    Tries the remote TaiwanCalendar first, then falls back to the bundled
    local file, then to an empty map (client uses the weekend rule).
    """
    cached = _holidays_cache.get(year)
    if cached is not None and cached.is_fresh:
        return cached.entries

    with _holidays_lock:
        cached = _holidays_cache.get(year)
        if cached is not None and cached.is_fresh:
            return cached.entries

        entries = _fetch_taiwan_calendar(year)
        if entries is None:
            entries = _load_local_holidays().get(str(year), {})
        _holidays_cache[year] = _HolidayCacheEntry(entries=entries)
        return entries


@router.get("/api/v1/holidays")
def get_holidays(
    request: Request,
    year: int | None = Query(default=None, description="Filter by year, e.g. 2026"),
    date: str | None = Query(
        default=None, description="Filter by a single date in YYYY-MM-DD"
    ),
) -> dict:
    """Returns the Taiwan national holiday calendar (from open data).

    - ?year=2026: returns that year's holiday/make-up entries as a list.
    - ?date=2026-02-18: returns ``{date, isHoliday, name}``; dates without an
      explicit entry fall back to the weekend rule (Sat/Sun = holiday).
    - No params: defaults to the current year (avoids fetching many years).
    """
    if date is not None:
        try:
            parsed = datetime.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid date format, expected YYYY-MM-DD")
        entries = _get_holidays_for_year(parsed.year)
        info = entries.get(parsed.isoformat())
        if info is not None:
            is_holiday = bool(info.get("isHoliday", True))
            name = info.get("name")
        else:
            is_holiday = parsed.weekday() >= 5  # weekend fallback
            name = None
        return {"date": parsed.isoformat(), "isHoliday": is_holiday, "name": name}

    target_year = year if year is not None else datetime.now().year
    entries = _get_holidays_for_year(target_year)
    holiday_list = [
        {"date": iso, "isHoliday": info["isHoliday"], "name": info.get("name")}
        for iso, info in sorted(entries.items())
    ]
    return {
        "year": target_year,
        "source": "ruyut/TaiwanCalendar",
        "holidays": holiday_list,
    }
