"""Whole-city live bus snapshots for the 全公車地圖 map screen.

The per-route endpoint answers "where are this route's buses"; a city map asks
"where is everything", which the $filter path cannot serve: Taipei's 416
RouteUIDs chunk into 17 requests and every TDX call is serialized behind a
process-wide 0.5 s interval. The unfiltered collection is one page per city
instead (measured 2026-09-06: Taipei 777 items, NewTaipei 633, Taichung 328,
InterCity 1295 over two pages), so this module pulls that once per city per TTL
and hands the same snapshot to every client.

Identity is the delicate part. Items are resolved with
``_tdx_item_to_local_strict``, which never falls back to treating a RouteUID as
a routeid: that fallback is only safe for callers that then drop ids missing
from a tracked route set, and a city feed has none. Buses that cannot be pinned
to one variant keep ``routeid: null`` and are described through a ``families``
entry instead, so they are still drawn, still labelled, and still matched by a
favourite of any family member.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from typing import Any

from app.config import Settings
from app.db import _stat_fingerprint, get_readonly_connection
from app.logging_utils import get_logger
from app.route_aliases import get_route_alias_index
from app.sync_realtime import _build_buses_payload, _tdx_item_to_local_strict

LOGGER = get_logger("city_buses")

# An item count this far below the previous one means the upstream probably
# truncated rather than that the fleet went home. Deliberately generous: an
# evening lull halves a fleet, it does not quarter it.
_TRUNCATION_DROP_RATIO = 0.25
_TRUNCATION_MIN_PREVIOUS = 50


@dataclass
class _CitySnapshot:
    payload: dict[str, Any]
    fetched_at: float
    updated_at: int
    last_modified: str | None
    item_count: int
    page_size: int
    page_size_calibrated: bool = False


@dataclass
class _RouteNames:
    names: dict[str, str]
    names_en: dict[str, str | None]
    fingerprint: tuple[int, int, int] | None = field(default=None)


class CityBusesService:
    def __init__(self, settings: Settings, client) -> None:
        self.settings = settings
        self.client = client
        self._snapshots: dict[str, _CitySnapshot] = {}
        self._snapshots_lock = threading.Lock()
        self._city_locks: dict[str, threading.Lock] = {}
        self._city_locks_guard = threading.Lock()
        self._names: dict[str, _RouteNames] = {}
        self._names_lock = threading.Lock()

    # -- public API -----------------------------------------------------------

    def get_city_buses(self, city_name: str, prefix: str) -> dict[str, Any]:
        cached = self._get_snapshot(city_name)
        if cached is not None and self._is_fresh(cached):
            return cached.payload

        lock = self._get_city_lock(city_name)
        blocking = cached is None or not self._is_usable(cached)
        if not lock.acquire(blocking=blocking):
            # A refresh is already running. Rather than queue behind a TDX call
            # that is serialized against every other one, hand back what we have
            # while it is still worth showing.
            return dict(cached.payload, stale=True)  # type: ignore[union-attr]

        try:
            cached = self._get_snapshot(city_name)
            if cached is not None and self._is_fresh(cached):
                return cached.payload
            return self._refresh(city_name, prefix, cached)
        finally:
            lock.release()

    # -- refresh --------------------------------------------------------------

    def _refresh(
        self,
        city_name: str,
        prefix: str,
        cached: _CitySnapshot | None,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        page_size = cached.page_size if cached else self.settings.city_buses_page_size
        try:
            response = self.client.fetch_city_realtime_buses(
                city_name,
                if_modified_since=cached.last_modified if cached else None,
                page_size=page_size,
            )
        except Exception as exc:
            if cached is not None and self._is_usable(cached):
                LOGGER.warning(
                    "city buses upstream failed city=%s age=%.0fs serving stale error=%s",
                    city_name,
                    time.monotonic() - cached.fetched_at,
                    exc,
                )
                return dict(cached.payload, stale=True)
            raise

        # A 304 carries an empty payload, not an empty city.
        if response.not_modified and cached is not None:
            with self._snapshots_lock:
                cached.fetched_at = time.monotonic()
                cached.updated_at = int(time.time())
                cached.payload["updated_at"] = cached.updated_at
                cached.payload["stale"] = False
            LOGGER.info(
                "city buses not modified city=%s buses=%s",
                city_name,
                len(cached.payload.get("buses", ())),
            )
            return cached.payload

        items = list(response.payload or [])
        page_size, items, capped = self._calibrate_page_size(
            city_name, items, page_size, cached
        )
        dropped_sharply = self._looks_truncated(
            len(items), cached.item_count if cached else None
        )
        if dropped_sharply:
            LOGGER.warning(
                "city buses item count dropped sharply city=%s items=%s previous=%s",
                city_name,
                len(items),
                cached.item_count if cached else None,
            )
        truncated = capped or dropped_sharply

        payload = self._build_payload(city_name, prefix, items, truncated=truncated)
        snapshot = _CitySnapshot(
            payload=payload,
            fetched_at=time.monotonic(),
            updated_at=payload["updated_at"],
            last_modified=response.last_modified,
            item_count=len(items),
            page_size=page_size,
            page_size_calibrated=(cached.page_size_calibrated if cached else False)
            or capped,
        )
        with self._snapshots_lock:
            self._snapshots[city_name] = snapshot

        LOGGER.info(
            "city buses refreshed city=%s status=%s items=%s buses=%s unresolved=%s "
            "families=%s duration_ms=%.0f",
            city_name,
            response.status_code,
            len(items),
            len(payload["buses"]),
            sum(1 for bus in payload["buses"] if bus["routeid"] is None),
            len(payload["families"]),
            (time.monotonic() - started_at) * 1000,
        )
        return payload

    def _calibrate_page_size(
        self,
        city_name: str,
        items: list[dict[str, Any]],
        page_size: int,
        cached: _CitySnapshot | None,
    ) -> tuple[int, list[dict[str, Any]], bool]:
        """Detect a silent server-side $top cap, once per city per process.

        ``fetch_paginated_items_conditional`` stops as soon as a page comes back
        shorter than requested, so a cap of N looks exactly like "that is the
        whole city". One probe past the end settles it: an item there means the
        page was cut, not complete.
        """
        if cached is not None and cached.page_size_calibrated:
            return page_size, items, False
        if not items or len(items) >= page_size:
            return page_size, items, False

        try:
            beyond = self.client.probe_city_realtime_buses(city_name, skip=len(items))
        except Exception:
            return page_size, items, False
        if not beyond:
            return page_size, items, False

        capped_size = max(1, len(items))
        LOGGER.warning(
            "city buses page cap detected city=%s cap=%s", city_name, capped_size
        )
        try:
            full = self.client.fetch_city_realtime_buses(
                city_name,
                page_size=capped_size,
                if_modified_since=None,
            )
        except Exception:
            return capped_size, items, True
        return capped_size, list(full.payload or items), True

    @staticmethod
    def _looks_truncated(count: int, previous: int | None) -> bool:
        if previous is None or previous < _TRUNCATION_MIN_PREVIOUS:
            return False
        return count < previous * _TRUNCATION_DROP_RATIO

    # -- payload --------------------------------------------------------------

    def _build_payload(
        self,
        city_name: str,
        prefix: str,
        items: list[dict[str, Any]],
        *,
        truncated: bool,
    ) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        identity: dict[str, tuple[str | None, str]] = {}
        for item in items:
            routeid, route_uid, _direction = _tdx_item_to_local_strict(
                city_name, item, settings=self.settings
            )
            if not route_uid and routeid is None:
                continue
            key = routeid if routeid is not None else f"uid:{route_uid}"
            identity.setdefault(key, (routeid, route_uid or ""))
            grouped[key].append(item)

        names = self._route_names(prefix)
        alias = get_route_alias_index(self.settings)

        buses: list[dict[str, Any]] = []
        routes: dict[str, dict[str, Any]] = {}
        families: dict[str, dict[str, Any]] = {}

        for key, group in grouped.items():
            routeid, route_uid = identity[key]
            # Dedupe by plate per route, never across the whole city: one plate
            # can legitimately appear under two routes in the same snapshot.
            for bus in _build_buses_payload(group):
                bus["routeid"] = routeid
                bus["route_uid"] = route_uid or None
                buses.append(bus)

            if routeid is not None:
                routes.setdefault(
                    routeid,
                    {
                        "name": names.names.get(routeid) or routeid,
                        "name_en": names.names_en.get(routeid),
                        "route_uid": route_uid or None,
                    },
                )
            elif route_uid and route_uid not in families:
                families[route_uid] = self._family_entry(route_uid, names, alias)

        return {
            "city": city_name,
            "prefix": prefix,
            "updated_at": int(time.time()),
            "ttl": self.settings.city_buses_cache_ttl,
            "stale": False,
            "truncated": truncated,
            "buses": buses,
            "routes": routes,
            "families": families,
        }

    def _family_entry(
        self,
        route_uid: str,
        names: _RouteNames,
        alias,
    ) -> dict[str, Any]:
        """Describe a RouteUID whose buses cannot be pinned to one variant.

        Stub rows (``name == routeid``, no stops, paths named "Unknown") are
        leftovers of the static shape feed. They carry geometry, so they are a
        fine source for the route line, but they have nothing to show in route
        detail, hence the split between ``geometry_routeid`` and
        ``stops_routeid``.
        """
        candidates = [
            routeid
            for routeid in alias.family_routeids(route_uid)
            if names.names.get(routeid) not in (None, routeid)
        ]
        stops_routeid = min(
            candidates,
            key=lambda routeid: (len(names.names[routeid]), names.names[routeid]),
            default=None,
        )
        name = names.names[stops_routeid] if stops_routeid else route_uid
        name_en = names.names_en.get(stops_routeid) if stops_routeid else None

        canonical = alias.canonical(route_uid)
        geometry_routeid = canonical if canonical in names.names else stops_routeid

        return {
            "name": name,
            "name_en": name_en,
            "stops_routeid": stops_routeid,
            "geometry_routeid": geometry_routeid,
            "routeids": candidates,
        }

    def _route_names(self, prefix: str) -> _RouteNames:
        """Bilingual route names for one authority, refreshed when the DB is swapped."""
        db_path = Path(self.settings.db_path)
        fingerprint = _stat_fingerprint(db_path)
        with self._names_lock:
            cached = self._names.get(prefix)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached

        try:
            with get_readonly_connection(db_path) as connection:
                rows = connection.execute(
                    "SELECT routeid, name, name_en FROM routes WHERE routeid LIKE ?",
                    (f"{prefix}%",),
                ).fetchall()
            names = {row["routeid"]: row["name"] for row in rows}
            names_en = {row["routeid"]: row["name_en"] for row in rows}
        except Exception:
            LOGGER.warning(
                "city buses route name lookup failed prefix=%s", prefix, exc_info=True
            )
            return _RouteNames(names={}, names_en={})

        route_names = _RouteNames(
            names=names,
            names_en=names_en,
            fingerprint=fingerprint,
        )
        with self._names_lock:
            self._names[prefix] = route_names
        return route_names

    # -- cache plumbing -------------------------------------------------------

    def _get_snapshot(self, city_name: str) -> _CitySnapshot | None:
        with self._snapshots_lock:
            return self._snapshots.get(city_name)

    def _is_fresh(self, snapshot: _CitySnapshot) -> bool:
        age = time.monotonic() - snapshot.fetched_at
        return age < self.settings.city_buses_cache_ttl

    def _is_usable(self, snapshot: _CitySnapshot) -> bool:
        age = time.monotonic() - snapshot.fetched_at
        return age < self.settings.city_buses_stale_max_seconds

    def _get_city_lock(self, city_name: str) -> threading.Lock:
        with self._city_locks_guard:
            return self._city_locks.setdefault(city_name, threading.Lock())
