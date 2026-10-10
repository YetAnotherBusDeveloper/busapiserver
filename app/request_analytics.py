from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
import re
import threading
import time
from typing import Any

from app.db import get_connection
from app.logging_utils import get_logger

LOGGER = get_logger("request_analytics")


MAX_USER_AGENT_LENGTH = 500
SKIP_ANALYTICS_ENDPOINTS = frozenset(
    {
        "/analytics",
        "/api/v1/analytics",
        "/admin/analytics",
        "/api/v1/admin/analytics",
        # The map polls this every ~15 s per open screen; recording each poll
        # would bury every other endpoint in the analytics table.
        "/api/v1/cities/{city}/buses",
    }
)
SKIP_ANALYTICS_PREFIXES = (
    "/info",
)

_APP_USER_AGENT_PATTERN = re.compile(
    r"^YABus/(?P<version>[^-\s()]+)(?:-(?P<commit>[A-Za-z0-9]+))?\s*\((?P<platform>[^)]+)\)\s*$"
)
_ANDROID_PATTERN = re.compile(r"Android (?P<version>[\d.]+)", re.IGNORECASE)
_IOS_PATTERN = re.compile(
    r"(?:iPhone|iPad|iPod)(?: OS|; CPU(?: iPhone)? OS) (?P<version>[\d_]+)",
    re.IGNORECASE,
)
_WINDOWS_PATTERN = re.compile(r"Windows NT (?P<version>[\d.]+)", re.IGNORECASE)
_MACOS_PATTERN = re.compile(r"Mac OS X (?P<version>[\d_]+)", re.IGNORECASE)
_CHROMEOS_PATTERN = re.compile(r"CrOS [^ ]+ (?P<version>[\d.]+)", re.IGNORECASE)
_BROWSER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Edge", re.compile(r"Edg(?:A|iOS)?/(?P<version>[\d.]+)")),
    ("Opera", re.compile(r"OPR/(?P<version>[\d.]+)")),
    ("Chrome", re.compile(r"Chrome/(?P<version>[\d.]+)")),
    ("Firefox", re.compile(r"Firefox/(?P<version>[\d.]+)")),
    ("Safari", re.compile(r"Version/(?P<version>[\d.]+).+Safari/")),
)


@dataclass(frozen=True)
class ParsedUserAgent:
    client_family: str
    platform_name: str | None = None
    system_name: str | None = None
    system_version: str | None = None
    app_version: str | None = None
    app_commit_hash: str | None = None
    browser_name: str | None = None
    browser_version: str | None = None


def should_record_analytics(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    if endpoint in SKIP_ANALYTICS_ENDPOINTS:
        return False
    return not endpoint.startswith(SKIP_ANALYTICS_PREFIXES)


def parse_user_agent(user_agent: str | None) -> ParsedUserAgent:
    normalized = (user_agent or "").strip()
    if not normalized:
        return ParsedUserAgent(client_family="unknown", platform_name="unknown")

    app_match = _APP_USER_AGENT_PATTERN.match(normalized)
    if app_match:
        platform_name = _clean_label(app_match.group("platform"))
        system_name, system_version = _detect_system(platform_name)
        return ParsedUserAgent(
            client_family="app",
            platform_name=platform_name or "app",
            system_name=system_name or platform_name,
            system_version=system_version,
            app_version=_clean_label(app_match.group("version")),
            app_commit_hash=_clean_label(app_match.group("commit")),
        )

    browser_name, browser_version = _detect_browser(normalized)
    if browser_name or "Mozilla/" in normalized:
        system_name, system_version = _detect_system(normalized)
        return ParsedUserAgent(
            client_family="web",
            platform_name=_detect_web_platform(normalized),
            system_name=system_name or "Unknown",
            system_version=system_version,
            browser_name=browser_name or "Unknown",
            browser_version=browser_version,
        )

    return ParsedUserAgent(client_family="unknown", platform_name="unknown")


def record_request_analytics(
    db_path: str | Path,
    *,
    method: str,
    endpoint: str,
    path: str,
    status_code: int,
    user_agent: str | None,
    requested_at: int | None = None,
) -> None:
    record_request_analytics_batch(db_path, [dict(
        method=method, endpoint=endpoint, path=path, status_code=status_code,
        user_agent=user_agent, requested_at=requested_at,
    )])


def _analytics_row(
    *, method: str, endpoint: str, path: str, status_code: int,
    user_agent: str | None, requested_at: int | None = None,
) -> tuple[object, ...]:
    parsed = parse_user_agent(user_agent)
    safe_user_agent = (user_agent or "").strip()[:MAX_USER_AGENT_LENGTH]
    return (
        int(time.time() if requested_at is None else requested_at),
        method, endpoint, path, int(status_code), parsed.client_family,
        parsed.platform_name, parsed.system_name, parsed.system_version,
        parsed.app_version, parsed.app_commit_hash, parsed.browser_name,
        parsed.browser_version, safe_user_agent,
    )


def record_request_analytics_batch(db_path: str | Path, events: list[dict[str, Any]]) -> None:
    rows = [_analytics_row(**event) for event in events]
    with get_connection(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO request_analytics (
                requested_at,
                method,
                endpoint,
                path,
                status_code,
                client_family,
                platform_name,
                system_name,
                system_version,
                app_version,
                app_commit_hash,
                browser_name,
                browser_version,
                user_agent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()


class AnalyticsWriter:
    """Bounded, best-effort telemetry; no SQLite work on a request's event loop."""

    def __init__(
        self, db_path: str | Path, *, capacity: int = 2048, batch_size: int = 64,
        flush_interval: float = 0.25,
    ) -> None:
        if capacity <= 0 or batch_size <= 0 or flush_interval <= 0:
            raise ValueError("Analytics queue and flush limits must be positive")
        self.db_path = db_path
        self._queue: Queue[dict[str, Any]] = Queue(maxsize=capacity)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._stop = threading.Event()
        self._shutdown_deadline = float("inf")
        self.dropped = 0
        self.written = 0
        self._thread = threading.Thread(target=self._run, name="request-analytics", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(
        self, *, method: str, endpoint: str, path: str, status_code: int,
        user_agent: str | None,
    ) -> bool:
        if self._stop.is_set():
            return False
        event = dict(
            method=method, endpoint=endpoint, path=path[:2048], status_code=status_code,
            user_agent=(user_agent or "")[:MAX_USER_AGENT_LENGTH], requested_at=int(time.time()),
        )
        try:
            self._queue.put_nowait(event)
            return True
        except Full:
            self.dropped += 1
            # Log sparsely even during an extended database outage.
            if self.dropped & (self.dropped - 1) == 0:
                LOGGER.warning("analytics queue full dropped=%s", self.dropped)
            return False

    def close(self, timeout: float = 6.0) -> bool:
        self._shutdown_deadline = time.monotonic() + timeout
        self._stop.set()
        self._thread.join(timeout=timeout)
        finished = not self._thread.is_alive()
        if not finished:
            LOGGER.warning("analytics shutdown deadline reached pending=%s", self._queue.qsize())
        return finished

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            if time.monotonic() >= self._shutdown_deadline:
                break
            try:
                batch = [self._queue.get(timeout=self._flush_interval)]
            except Empty:
                continue
            deadline = time.monotonic() + self._flush_interval
            while len(batch) < self._batch_size:
                try:
                    if self._stop.is_set():
                        batch.append(self._queue.get_nowait())
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        batch.append(self._queue.get(timeout=remaining))
                except Empty:
                    break
            try:
                record_request_analytics_batch(self.db_path, batch)
                self.written += len(batch)
            except Exception:
                self.dropped += len(batch)
                LOGGER.exception("analytics batch write failed count=%s", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()
        pending = self._queue.qsize()
        if pending:
            self.dropped += pending
            LOGGER.warning("analytics shutdown discarded queued events count=%s", pending)


def build_analytics_report(
    db_path: str | Path,
    *,
    days: int = 7,
    limit: int = 20,
) -> dict[str, object]:
    now = int(time.time())
    since_at = now - (days * 86400) if days > 0 else None
    base_conditions = ["requested_at >= ?"] if since_at is not None else []
    base_args: tuple[object, ...] = ((since_at,) if since_at is not None else ())
    daily_limit = max(7, min(days if days > 0 else 30, 90))

    with get_connection(db_path) as connection:
        summary_row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total_requests,
                COUNT(DISTINCT endpoint) AS unique_endpoints,
                COUNT(DISTINCT user_agent) AS unique_user_agents,
                COUNT(DISTINCT NULLIF(app_version, '')) AS unique_app_versions,
                COUNT(DISTINCT NULLIF(browser_name, '')) AS unique_browsers,
                MAX(requested_at) AS last_seen_at
            FROM request_analytics
            {_where_sql(base_conditions)}
            """,
            base_args,
        ).fetchone()

        client_types = _fetch_rows(
            connection,
            f"""
            SELECT
                client_family AS client_type,
                COUNT(*) AS request_count
            FROM request_analytics
            {_where_sql(base_conditions)}
            GROUP BY client_family
            ORDER BY request_count DESC, client_family ASC
            LIMIT ?
            """,
            (*base_args, limit),
        )

        platforms = _fetch_rows(
            connection,
            f"""
            SELECT
                COALESCE(NULLIF(platform_name, ''), 'unknown') AS platform,
                COUNT(*) AS request_count
            FROM request_analytics
            {_where_sql(base_conditions)}
            GROUP BY platform
            ORDER BY request_count DESC, platform ASC
            LIMIT ?
            """,
            (*base_args, limit),
        )

        systems = _fetch_rows(
            connection,
            f"""
            SELECT
                COALESCE(NULLIF(system_name, ''), 'unknown') AS system,
                COALESCE(NULLIF(system_version, ''), '') AS system_version,
                COUNT(*) AS request_count
            FROM request_analytics
            {_where_sql(base_conditions)}
            GROUP BY system, system_version
            ORDER BY request_count DESC, system ASC, system_version ASC
            LIMIT ?
            """,
            (*base_args, limit),
        )

        daily_rows = _fetch_rows(
            connection,
            f"""
            SELECT
                strftime('%Y-%m-%d', requested_at, 'unixepoch', 'localtime') AS date,
                COUNT(*) AS request_count
            FROM request_analytics
            {_where_sql(base_conditions)}
            GROUP BY date
            ORDER BY date DESC
            LIMIT ?
            """,
            (*base_args, daily_limit),
        )

        endpoints = _fetch_rows(
            connection,
            f"""
            SELECT
                endpoint,
                COUNT(*) AS request_count,
                SUM(CASE WHEN client_family = 'app' THEN 1 ELSE 0 END) AS app_requests,
                SUM(CASE WHEN client_family = 'web' THEN 1 ELSE 0 END) AS web_requests,
                SUM(CASE WHEN client_family = 'unknown' THEN 1 ELSE 0 END) AS unknown_requests,
                MAX(requested_at) AS last_seen_at
            FROM request_analytics
            {_where_sql(base_conditions)}
            GROUP BY endpoint
            ORDER BY request_count DESC, endpoint ASC
            LIMIT ?
            """,
            (*base_args, limit),
        )

        app_usage = _fetch_rows(
            connection,
            f"""
            SELECT
                COALESCE(NULLIF(app_version, ''), 'unknown') AS app_version,
                COALESCE(NULLIF(app_commit_hash, ''), '') AS app_commit_hash,
                COALESCE(NULLIF(platform_name, ''), 'unknown') AS platform,
                COALESCE(NULLIF(system_name, ''), 'unknown') AS system,
                endpoint,
                COUNT(*) AS request_count,
                MAX(requested_at) AS last_seen_at
            FROM request_analytics
            {_where_sql([*base_conditions, "client_family = 'app'"])}
            GROUP BY app_version, app_commit_hash, platform, system, endpoint
            ORDER BY request_count DESC, last_seen_at DESC, app_version ASC
            LIMIT ?
            """,
            (*base_args, limit),
        )

        web_usage = _fetch_rows(
            connection,
            f"""
            SELECT
                COALESCE(NULLIF(browser_name, ''), 'Unknown') AS browser_name,
                COALESCE(NULLIF(browser_version, ''), '') AS browser_version,
                COALESCE(NULLIF(platform_name, ''), 'unknown') AS platform,
                COALESCE(NULLIF(system_name, ''), 'unknown') AS system,
                COALESCE(NULLIF(system_version, ''), '') AS system_version,
                endpoint,
                COUNT(*) AS request_count,
                MAX(requested_at) AS last_seen_at
            FROM request_analytics
            {_where_sql([*base_conditions, "client_family = 'web'"])}
            GROUP BY browser_name, browser_version, platform, system, system_version, endpoint
            ORDER BY request_count DESC, last_seen_at DESC, browser_name ASC
            LIMIT ?
            """,
            (*base_args, limit),
        )

        recent_requests = _fetch_rows(
            connection,
            f"""
            SELECT
                requested_at,
                method,
                endpoint,
                status_code,
                client_family AS client_type,
                COALESCE(NULLIF(platform_name, ''), 'unknown') AS platform,
                COALESCE(NULLIF(system_name, ''), 'unknown') AS system,
                COALESCE(NULLIF(system_version, ''), '') AS system_version,
                COALESCE(NULLIF(app_version, ''), '') AS app_version,
                COALESCE(NULLIF(browser_name, ''), '') AS browser_name,
                COALESCE(NULLIF(browser_version, ''), '') AS browser_version,
                user_agent
            FROM request_analytics
            {_where_sql(base_conditions)}
            ORDER BY requested_at DESC, id DESC
            LIMIT ?
            """,
            (*base_args, limit),
        )

    return {
        "generated_at": now,
        "period_days": days,
        "since_at": since_at,
        "summary": {
            "total_requests": int(summary_row["total_requests"] or 0),
            "unique_endpoints": int(summary_row["unique_endpoints"] or 0),
            "unique_user_agents": int(summary_row["unique_user_agents"] or 0),
            "unique_app_versions": int(summary_row["unique_app_versions"] or 0),
            "unique_browsers": int(summary_row["unique_browsers"] or 0),
            "last_seen_at": summary_row["last_seen_at"],
        },
        "client_types": client_types,
        "platforms": platforms,
        "systems": systems,
        "daily": list(reversed(daily_rows)),
        "endpoints": endpoints,
        "app_usage": app_usage,
        "web_usage": web_usage,
        "recent_requests": recent_requests,
    }


def _fetch_rows(connection, sql: str, args: tuple[object, ...]) -> list[dict[str, object]]:
    return [dict(row) for row in connection.execute(sql, args).fetchall()]


def _where_sql(conditions: list[str]) -> str:
    if not conditions:
        return ""
    return f"WHERE {' AND '.join(conditions)}"


def _clean_label(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _detect_browser(user_agent: str) -> tuple[str | None, str | None]:
    for browser_name, pattern in _BROWSER_PATTERNS:
        match = pattern.search(user_agent)
        if match:
            return browser_name, _clean_label(match.group("version"))
    return None, None


def _detect_web_platform(user_agent: str) -> str:
    lowered = user_agent.lower()
    if any(token in lowered for token in ("bot", "crawler", "spider", "slurp")):
        return "bot-web"
    if "ipad" in lowered or "tablet" in lowered:
        return "tablet-web"
    if any(token in lowered for token in ("iphone", "android", "mobile")):
        return "mobile-web"
    return "desktop-web"


def _detect_system(value: str | None) -> tuple[str | None, str | None]:
    normalized = (value or "").strip()
    if not normalized:
        return None, None

    android_match = _ANDROID_PATTERN.search(normalized)
    if android_match or "android" in normalized.lower():
        return "Android", _normalize_version((android_match.group("version") if android_match else None))

    ios_match = _IOS_PATTERN.search(normalized)
    if ios_match:
        return "iOS", _normalize_version(ios_match.group("version"))
    lowered = normalized.lower()
    if any(token in lowered for token in ("ios", "iphone", "ipad")):
        return "iOS", None

    windows_match = _WINDOWS_PATTERN.search(normalized)
    if windows_match or "windows" in lowered:
        return "Windows", _normalize_version((windows_match.group("version") if windows_match else None))

    macos_match = _MACOS_PATTERN.search(normalized)
    if macos_match or "mac os" in lowered or "macos" in lowered:
        return "macOS", _normalize_version((macos_match.group("version") if macos_match else None))

    chromeos_match = _CHROMEOS_PATTERN.search(normalized)
    if chromeos_match or "cros" in lowered or "chrome os" in lowered:
        return "ChromeOS", _normalize_version((chromeos_match.group("version") if chromeos_match else None))

    if "linux" in lowered:
        return "Linux", None
    return None, None


def _normalize_version(value: str | None) -> str | None:
    normalized = _clean_label(value)
    if normalized is None:
        return None
    return normalized.replace("_", ".")
