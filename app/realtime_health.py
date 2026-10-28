"""Notice when a city's realtime ETA feed goes quiet.

TDX can answer 200 with an empty list for a whole city while its static data
stays fine: Kaohsiung did this for days in 2026-10. Nothing errors, so only a
user report caught it. The monitor watches the ETA refreshes RealtimeService
already makes, so it costs no extra upstream requests, and posts to Discord once
a city has returned nothing for long enough, then again when data comes back.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from app.config import Settings
from app.discord_webhook import notify_realtime_feed
from app.logging_utils import get_logger


LOGGER = get_logger("realtime_health")

# One route TDX simply has no ETA for (a stub, a suspended line) must not look
# like a dead city, so the empty streak has to span several distinct routes.
MIN_EMPTY_ROUTES = 3
# Enough to show the scale in the alert; the set never needs to grow further.
_MAX_TRACKED_ROUTES = 100


@dataclass(frozen=True)
class RealtimeFeedEvent:
    city: str
    empty_since: int
    route_count: int
    # None while the feed is still empty.
    recovered_at: int | None = None


@dataclass
class _EmptyStreak:
    since: float
    routeids: set[str] = field(default_factory=set)
    alerted: bool = False


class EmptyRealtimeMonitor:
    def __init__(
        self,
        settings: Settings,
        *,
        notify: Callable[[RealtimeFeedEvent], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._notify = notify or self._post_in_background
        self._clock = clock
        self._streaks: dict[str, _EmptyStreak] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        if self.settings.realtime_empty_alert_after_seconds <= 0:
            return False
        return bool(
            self.settings.realtime_alert_discord_webhook_url
            or self.settings.feedback_discord_webhook_url
        )

    def record(self, city: str, routeids: Iterable[str], item_count: int) -> None:
        """Feed in one ETA refresh that returned a body (not a 304).

        ``item_count`` is the number of rows that mapped onto ``routeids``.
        """
        if not self.enabled:
            return

        now = self._clock()
        event: RealtimeFeedEvent | None = None
        with self._lock:
            if item_count > 0:
                streak = self._streaks.pop(city, None)
                if streak is not None and streak.alerted:
                    event = RealtimeFeedEvent(
                        city=city,
                        empty_since=int(streak.since),
                        route_count=len(streak.routeids),
                        recovered_at=int(now),
                    )
            else:
                streak = self._streaks.setdefault(city, _EmptyStreak(since=now))
                for routeid in routeids:
                    if len(streak.routeids) >= _MAX_TRACKED_ROUTES:
                        break
                    streak.routeids.add(routeid)
                if (
                    not streak.alerted
                    and now - streak.since >= self.settings.realtime_empty_alert_after_seconds
                    and len(streak.routeids) >= MIN_EMPTY_ROUTES
                ):
                    streak.alerted = True
                    event = RealtimeFeedEvent(
                        city=city,
                        empty_since=int(streak.since),
                        route_count=len(streak.routeids),
                    )

        if event is None:
            return
        if event.recovered_at is None:
            LOGGER.warning(
                "realtime feed empty city=%s since=%s routes=%s",
                event.city, event.empty_since, event.route_count,
            )
        else:
            LOGGER.info(
                "realtime feed recovered city=%s empty_since=%s",
                event.city, event.empty_since,
            )
        self._notify(event)

    def _post_in_background(self, event: RealtimeFeedEvent) -> None:
        # Callers hold the city refresh lock; a slow webhook must not stall it.
        def post() -> None:
            try:
                notify_realtime_feed(
                    self.settings,
                    city=event.city,
                    empty_since=event.empty_since,
                    route_count=event.route_count,
                    recovered_at=event.recovered_at,
                )
            except Exception:
                LOGGER.exception("realtime feed alert failed city=%s", event.city)

        try:
            threading.Thread(target=post, name=f"realtime-alert-{event.city}", daemon=True).start()
        except Exception as exc:
            LOGGER.warning("realtime feed alert could not start city=%s error=%s", event.city, exc)
