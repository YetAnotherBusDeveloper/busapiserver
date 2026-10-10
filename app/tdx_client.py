from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import random
import threading
import time
from typing import Any

import requests

from app.config import Settings, from_intercity_routeid, is_intercity_city
from app.logging_utils import get_logger
from app.route_aliases import get_route_alias_index
from app.request_budget import budget_lock, budget_sleep, remaining_seconds, request_timeout
from app.tdx_auth import TDXTokenManager

LOGGER = get_logger("tdx_client")


@dataclass
class TDXJSONResponse:
    payload: Any
    status_code: int
    last_modified: str | None

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


class TDXClient:
    def __init__(self, settings: Settings, token_manager: TDXTokenManager) -> None:
        self.settings = settings
        self.token_manager = token_manager
        self._session = requests.Session()
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0

    def close(self) -> None:
        self._session.close()

    def _respect_min_interval(self) -> None:
        if self.settings.tdx_min_request_interval <= 0:
            return

        with budget_lock(self._request_lock):
            now = time.monotonic()
            if now < self._next_request_at:
                budget_sleep(self._next_request_at - now)
            self._next_request_at = time.monotonic() + self.settings.tdx_min_request_interval

    def _retry_delay(self, response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after).timestamp()
                        return max(0.0, retry_at - time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass

        base_delay = self.settings.tdx_retry_backoff * (2 ** max(0, attempt - 1))
        jitter = random.uniform(0.0, max(0.25, self.settings.tdx_retry_backoff))
        return base_delay + jitter

    def _request_json_with_meta(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retry_on_401: bool = True,
        if_modified_since: str | None = None,
        allow_not_modified: bool = False,
    ) -> TDXJSONResponse:
        url = f"{self.settings.tdx_base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self.settings.tdx_retry_attempts + 1):
            remaining_seconds()
            self._respect_min_interval()

            headers = {
                "Authorization": f"Bearer {self.token_manager.get_access_token()}",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            }
            if if_modified_since:
                headers["If-Modified-Since"] = if_modified_since
            response = self._session.get(
                url,
                headers=headers,
                params=params,
                timeout=request_timeout(self.settings.tdx_request_timeout),
            )
            remaining_seconds()
            if response.status_code == 401 and retry_on_401:
                self.token_manager.invalidate()
                retry_on_401 = False
                continue

            if response.status_code == 304 and allow_not_modified:
                return TDXJSONResponse(
                    payload=None,
                    status_code=response.status_code,
                    last_modified=response.headers.get("Last-Modified"),
                )

            if response.status_code == 429 and attempt < self.settings.tdx_retry_attempts:
                delay = self._retry_delay(response, attempt)
                LOGGER.warning(
                    "429 rate limited for %s, retrying in %.1fs (attempt %s/%s)",
                    path,
                    delay,
                    attempt,
                    self.settings.tdx_retry_attempts,
                )
                budget_sleep(delay)
                continue

            if response.status_code >= 500 and attempt < self.settings.tdx_retry_attempts:
                delay = self._retry_delay(response, attempt)
                LOGGER.warning(
                    "upstream %s for %s, retrying in %.1fs (attempt %s/%s)",
                    response.status_code,
                    path,
                    delay,
                    attempt,
                    self.settings.tdx_retry_attempts,
                )
                budget_sleep(delay)
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                last_error = exc
                break
            return TDXJSONResponse(
                payload=response.json(),
                status_code=response.status_code,
                last_modified=response.headers.get("Last-Modified"),
            )

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Request failed without a response for {path}")

    def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        return self._request_json_with_meta(
            path,
            params=params,
            retry_on_401=retry_on_401,
        ).payload

    def _normalize_items(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return list(payload)
        if isinstance(payload, dict):
            return list(payload.get("Items") or [])
        return []

    def _bus_resource_path(self, city: str, resource: str) -> str:
        if is_intercity_city(city):
            return f"/v2/Bus/{resource}/InterCity"
        return f"/v2/Bus/{resource}/City/{city}"

    def _normalize_routeids_for_city(self, city: str, routeids: Iterable[str]) -> list[str]:
        """Turn local routeids into the SubRouteUIDs TDX filters on.

        A merged route absorbed one SubRouteUID per direction, and TDX realtime
        is filtered by SubRouteUID, so every absorbed id has to be sent or the
        return direction reports no arrivals at all. Filtering on RouteUID
        instead is not an option: under 公路客運 a single RouteUID also covers
        the lettered variants (1815A … 1815G).
        """
        alias_index = get_route_alias_index(self.settings)
        normalized: list[str] = []
        seen: set[str] = set()
        for routeid in routeids:
            cleaned = routeid.strip()
            if not cleaned:
                continue
            for subroute_uid in alias_index.subroute_uids(cleaned):
                mapped = (
                    from_intercity_routeid(subroute_uid)
                    if is_intercity_city(city)
                    else subroute_uid
                )
                if mapped in seen:
                    continue
                seen.add(mapped)
                normalized.append(mapped)
        return normalized

    def _normalize_route_uids_for_city(self, city: str, routeids: Iterable[str]) -> list[str]:
        """Turn local routeids into the RouteUIDs TDX filters on.

        雙北 realtime items stopped carrying SubRouteUID, so a SubRouteUID
        filter matches nothing there — RouteUID is the only id those feeds tag
        reliably. Unmapped routeids fall back to themselves, which is exact
        for identity-style ids and at worst an empty match for the rest.
        Variant families collapse onto one RouteUID, so duplicates are
        expected and dropped.
        """
        alias_index = get_route_alias_index(self.settings)
        normalized: list[str] = []
        seen: set[str] = set()
        for routeid in routeids:
            cleaned = routeid.strip()
            if not cleaned:
                continue
            canonical = alias_index.canonical(cleaned)
            route_uid = alias_index.route_uid_for(canonical) or canonical
            mapped = (
                from_intercity_routeid(route_uid)
                if is_intercity_city(city)
                else route_uid
            )
            if mapped in seen:
                continue
            seen.add(mapped)
            normalized.append(mapped)
        return normalized

    def _uses_routeuid_filter(self, city: str) -> bool:
        return city in getattr(self.settings, "tdx_routeuid_filter_cities", ())

    def _build_subroute_filter(
        self,
        routeids: Iterable[str],
        *,
        field: str = "SubRouteUID",
    ) -> str:
        clauses = []
        for routeid in routeids:
            escaped = routeid.replace("'", "''")
            clauses.append(f"{field} eq '{escaped}'")
        return " or ".join(clauses)

    def _chunk_routeids_for_filter(
        self,
        routeids: Iterable[str],
        *,
        field: str = "SubRouteUID",
    ) -> list[list[str]]:
        chunks: list[list[str]] = []
        current_chunk: list[str] = []
        current_length = 0

        for routeid in routeids:
            escaped = routeid.replace("'", "''")
            clause = f"{field} eq '{escaped}'"
            clause_length = len(clause) if not current_chunk else len(" or ") + len(clause)
            if current_chunk and (len(current_chunk) >= 25 or current_length + clause_length > 1500):
                chunks.append(current_chunk)
                current_chunk = [routeid]
                current_length = len(clause)
                continue

            current_chunk.append(routeid)
            current_length += clause_length

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def fetch_paginated_items(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        skip = 0

        while True:
            page_params = {
                "$top": page_size,
                "$skip": skip,
                "$format": "JSON",
            }
            if params:
                page_params.update(params)

            payload = self._request_json(path, params=page_params)
            batch = self._normalize_items(payload)
            if not batch and not isinstance(payload, (list, dict)):
                break

            items.extend(batch)
            if len(batch) < page_size:
                break
            skip += len(batch)

        return items

    def fetch_paginated_items_conditional(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 1000,
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
        items: list[dict[str, Any]] = []
        skip = 0

        while True:
            page_params = {
                "$top": page_size,
                "$skip": skip,
                "$format": "JSON",
            }
            if params:
                page_params.update(params)

            page = self._request_json_with_meta(
                path,
                params=page_params,
                if_modified_since=if_modified_since if skip == 0 else None,
                allow_not_modified=True,
            )

            if page.not_modified:
                return TDXJSONResponse(
                    payload=[],
                    status_code=page.status_code,
                    last_modified=page.last_modified,
                )

            batch = self._normalize_items(page.payload)
            if not batch and not isinstance(page.payload, (list, dict)):
                break

            items.extend(batch)
            if len(batch) < page_size:
                return TDXJSONResponse(
                    payload=items,
                    status_code=page.status_code,
                    last_modified=page.last_modified,
                )
            skip += len(batch)

        return TDXJSONResponse(
            payload=items,
            status_code=200,
            last_modified=None,
        )

    def fetch_routes(self, city: str) -> list[dict[str, Any]]:
        return self.fetch_paginated_items(self._bus_resource_path(city, "Route"))

    def fetch_stop_of_route(self, city: str) -> list[dict[str, Any]]:
        return self.fetch_paginated_items(self._bus_resource_path(city, "StopOfRoute"))

    def fetch_shapes(self, city: str) -> list[dict[str, Any]]:
        return self.fetch_paginated_items(self._bus_resource_path(city, "Shape"))

    def fetch_estimated_time_of_arrival(
        self,
        city: str,
        routeid: str,
    ) -> list[dict[str, Any]]:
        return self.fetch_estimated_time_of_arrival_batch(city, [routeid]).payload or []

    def fetch_estimated_time_of_arrival_batch(
        self,
        city: str,
        routeids: Iterable[str],
        *,
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
        if self._uses_routeuid_filter(city):
            return self._fetch_subroute_batch(
                self._bus_resource_path(city, "EstimatedTimeOfArrival"),
                self._normalize_route_uids_for_city(city, routeids),
                field="RouteUID",
                if_modified_since=if_modified_since,
            )
        normalized_routeids = self._normalize_routeids_for_city(city, routeids)
        return self._fetch_subroute_batch(
            self._bus_resource_path(city, "EstimatedTimeOfArrival"),
            normalized_routeids,
            if_modified_since=if_modified_since,
        )

    def _fetch_subroute_batch(
        self,
        path: str,
        routeids: Iterable[str],
        *,
        field: str = "SubRouteUID",
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
        deduped_routeids: list[str] = []
        seen = set()
        for routeid in routeids:
            cleaned = routeid.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            deduped_routeids.append(cleaned)

        if not deduped_routeids:
            return TDXJSONResponse(payload=[], status_code=200, last_modified=None)

        chunks = self._chunk_routeids_for_filter(deduped_routeids, field=field)
        if len(chunks) == 1:
            response = self._request_json_with_meta(
                path,
                params={
                    "$filter": self._build_subroute_filter(chunks[0], field=field),
                    "$format": "JSON",
                    "$top": 2000,
                },
                if_modified_since=if_modified_since,
                allow_not_modified=bool(if_modified_since),
            )
            if response.not_modified:
                return TDXJSONResponse(
                    payload=[],
                    status_code=response.status_code,
                    last_modified=response.last_modified,
                )
            return TDXJSONResponse(
                payload=self._normalize_items(response.payload),
                status_code=response.status_code,
                last_modified=response.last_modified,
            )

        items: list[dict[str, Any]] = []
        for chunk in chunks:
            response = self._request_json_with_meta(
                path,
                params={
                    "$filter": self._build_subroute_filter(chunk, field=field),
                    "$format": "JSON",
                    "$top": 2000,
                },
            )
            items.extend(self._normalize_items(response.payload))

        return TDXJSONResponse(payload=items, status_code=200, last_modified=None)

    def fetch_realtime_by_frequency(
        self,
        city: str,
        routeid: str,
    ) -> list[dict[str, Any]]:
        return self.fetch_realtime_by_frequency_batch(city, [routeid]).payload or []

    def fetch_realtime_by_frequency_batch(
        self,
        city: str,
        routeids: Iterable[str],
        *,
        if_modified_since: str | None = None,
    ) -> TDXJSONResponse:
        if self._uses_routeuid_filter(city):
            return self._fetch_subroute_batch(
                self._bus_resource_path(city, "RealTimeByFrequency"),
                self._normalize_route_uids_for_city(city, routeids),
                field="RouteUID",
                if_modified_since=if_modified_since,
            )
        normalized_routeids = self._normalize_routeids_for_city(city, routeids)
        return self._fetch_subroute_batch(
            self._bus_resource_path(city, "RealTimeByFrequency"),
            normalized_routeids,
            if_modified_since=if_modified_since,
        )

    def fetch_alerts(self, city: str) -> list[dict[str, Any]]:
        return self.fetch_paginated_items(self._bus_resource_path(city, "Alert"))

    def fetch_city_realtime_buses(
        self,
        city: str,
        *,
        if_modified_since: str | None = None,
        page_size: int = 1000,
    ) -> TDXJSONResponse:
        """Every live bus in one city, unfiltered.

        The $filter path costs one request per 25 route ids (17 for Taipei);
        the unfiltered collection is a single page for every city measured so
        far, so a whole-city map polls it instead. Callers MUST branch on
        ``not_modified`` before reading ``payload``: a 304 comes back with an
        empty payload, not an empty city.
        """
        return self.fetch_paginated_items_conditional(
            self._bus_resource_path(city, "RealTimeByFrequency"),
            page_size=page_size,
            if_modified_since=if_modified_since,
        )

    def probe_city_realtime_buses(self, city: str, *, skip: int) -> int:
        """How many items exist just past ``skip``, capped at one.

        Paging stops as soon as a page comes back shorter than requested, so a
        server-side $top cap is indistinguishable from "that was the whole
        city". Asking for a single item past the end tells them apart. This is
        one raw request on purpose: paging it would walk the entire collection.
        """
        payload = self._request_json(
            self._bus_resource_path(city, "RealTimeByFrequency"),
            params={"$top": 1, "$skip": skip, "$format": "JSON"},
        )
        return len(self._normalize_items(payload))
