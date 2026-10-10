from __future__ import annotations

import threading
import time
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import requests
from urllib3.exceptions import InsecureRequestWarning

from app.logging_utils import get_logger
from app.request_budget import remaining_seconds, request_timeout


LOGGER = get_logger("ntpc_opendata")

NTPC_DATASET_BASE_URL = "https://data.ntpc.gov.tw/api/datasets"
NTPC_ROUTE_LIST_DATASET_ID = "0ee4e6bf-cee6-4ec8-8fe1-71f544015127"
NTPC_ESTIMATED_TIME_DATASET_ID = "07f7ccb3-ed00-43c4-966d-08e9dab24e95"

ROUTE_LIST_CACHE_TTL_SECONDS = 3600
ESTIMATED_TIME_CACHE_TTL_SECONDS = 20
ROUTE_LIST_PAGE_SIZE = 50000
ESTIMATED_TIME_PAGE_SIZE = 200000


@dataclass
class _DatasetCacheEntry:
    rows: list[dict[str, Any]]
    expires_at: float


class NtpcOpenDataClient:
    """Client for New Taipei City public bus OpenData datasets."""

    def __init__(self, *, request_timeout: int = 30) -> None:
        self.request_timeout = request_timeout
        self._session = requests.Session()
        self._cache: dict[str, _DatasetCacheEntry] = {}
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        self._session.close()

    def fetch_estimated_time_of_arrival_by_subroute(
        self,
        routeids: Iterable[str],
    ) -> dict[str, list[dict[str, Any]]]:
        pathattribute_to_routeid: dict[str, str] = {}
        for routeid in routeids:
            pathattributeid = _local_routeid_to_pathattributeid(routeid)
            if pathattributeid is not None:
                pathattribute_to_routeid[pathattributeid] = routeid

        if not pathattribute_to_routeid:
            return {}

        official_to_local: dict[str, str] = {}
        for route in self._route_list_rows():
            pathattributeid = str(route.get("pathattributeid") or "").strip()
            local_routeid = pathattribute_to_routeid.get(pathattributeid)
            official_routeid = str(route.get("id") or "").strip()
            if local_routeid and official_routeid:
                official_to_local[official_routeid] = local_routeid

        if not official_to_local:
            return {routeid: [] for routeid in pathattribute_to_routeid.values()}

        rows_by_route = {routeid: [] for routeid in pathattribute_to_routeid.values()}
        for eta_row in self._estimated_time_rows():
            local_routeid = official_to_local.get(str(eta_row.get("routeid") or "").strip())
            if local_routeid is not None:
                rows_by_route.setdefault(local_routeid, []).append(eta_row)

        return rows_by_route

    def _route_list_rows(self) -> list[dict[str, Any]]:
        return self._cached_dataset_rows(
            NTPC_ROUTE_LIST_DATASET_ID,
            page_size=ROUTE_LIST_PAGE_SIZE,
            ttl_seconds=ROUTE_LIST_CACHE_TTL_SECONDS,
        )

    def _estimated_time_rows(self) -> list[dict[str, Any]]:
        return self._cached_dataset_rows(
            NTPC_ESTIMATED_TIME_DATASET_ID,
            page_size=ESTIMATED_TIME_PAGE_SIZE,
            ttl_seconds=ESTIMATED_TIME_CACHE_TTL_SECONDS,
        )

    def _cached_dataset_rows(
        self,
        dataset_id: str,
        *,
        page_size: int,
        ttl_seconds: int,
    ) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(dataset_id)
            if cached is not None and cached.expires_at > now:
                return list(cached.rows)

        rows = self._fetch_dataset_rows(dataset_id, page_size=page_size)
        with self._cache_lock:
            self._cache[dataset_id] = _DatasetCacheEntry(
                rows=rows,
                expires_at=time.monotonic() + ttl_seconds,
            )
        return list(rows)

    def _fetch_dataset_rows(self, dataset_id: str, *, page_size: int) -> list[dict[str, Any]]:
        url = f"{NTPC_DATASET_BASE_URL}/{dataset_id}/json"
        params = {"page": 0, "size": page_size}
        headers = {
            "Accept": "application/json",
            "User-Agent": "busapiserver/1.0",
        }
        try:
            response = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=request_timeout(self.request_timeout),
            )
        except requests.exceptions.SSLError:
            LOGGER.warning(
                "NTPC OpenData TLS verification failed for dataset=%s; retrying without verification",
                dataset_id,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                response = self._session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=request_timeout(self.request_timeout),
                    verify=False,
                )

        remaining_seconds()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]


def _local_routeid_to_pathattributeid(routeid: str) -> str | None:
    normalized = routeid.strip()
    if not normalized.upper().startswith("NWT"):
        return None
    pathattributeid = normalized[3:].strip()
    return pathattributeid or None
