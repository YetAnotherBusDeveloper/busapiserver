"""Origin-departure observations, separate from stop arrival estimates.

A missing GPS fix is not a cancellation or proof of non-departure. Only an
origin's explicit timetable hint identifies a pending trip; frequency entries
provide a grace period, never fabricated individual departures.
"""
from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

TAIPEI = timezone(timedelta(hours=8))
FRESH_SECONDS = 90
DEFAULT_GRACE_SECONDS = 300
MAX_PENDING_SECONDS = 6 * 3600


def timestamp(value: Any) -> int | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(parsed.replace(tzinfo=TAIPEI).timestamp() if parsed.tzinfo is None else parsed.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def observed_at(item: dict) -> int | None:
    for key in ("SrcUpdateTime", "UpdateTime", "DataTime"):
        value = timestamp(item.get(key))
        if value is not None:
            return value
    return None


def _clock_seconds(value: Any) -> int | None:
    try:
        parts = str(value).split(":")
        if len(parts) not in (2, 3):
            return None
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        if 0 <= hour < 48 and 0 <= minute < 60 and 0 <= second < 60:
            return hour * 3600 + minute * 60 + second
    except (ValueError, TypeError):
        pass
    return None


def origin_departure_at(item: dict, now_ts: int) -> int | None:
    # NextBusTime includes its date. ScheduledTime may be a service-day clock.
    absolute = timestamp(item.get("ScheduledTime"))
    if absolute is not None:
        return absolute
    seconds = _clock_seconds(item.get("ScheduledTime"))
    if seconds is None:
        return timestamp(item.get("NextBusTime"))
    reference = datetime.fromtimestamp(observed_at(item) or now_ts, TAIPEI)
    midnight = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [int((midnight + timedelta(days=day, seconds=seconds)).timestamp()) for day in (-1, 0, 1)]
    # Anchor to the source date, not the client's fetch/reload time. This also
    # preserves 23:58 at 00:03 and service-day times such as 24:10.
    return min(candidates, key=lambda value: abs(value - reference.timestamp()))


def grace_seconds(headway_seconds: int | None) -> int:
    if headway_seconds is None or headway_seconds <= 0:
        return DEFAULT_GRACE_SECONDS
    return max(180, min(900, headway_seconds // 2))


def apply_departure_message(stop: dict, now_ts: int) -> None:
    """Legacy/native clients also get the correct elapsed/scheduled text.

    Run on copied cached responses too, without refreshing observation times.
    New clients use the structured fields to update at local minute boundaries.
    """
    departure = stop.get("departure")
    if not departure:
        return
    source = stop.get("updated_at")
    if stop.get("eta") is not None and source is not None and -15 <= now_ts - source <= FRESH_SECONDS:
        stop["message"] = ""
        return
    stop["eta"] = None
    stop["etas"] = []
    stop["buses"] = []
    scheduled = departure["scheduled_at"]
    clock = lambda value: datetime.fromtimestamp(value, TAIPEI).strftime("%H:%M")
    if now_ts < scheduled:
        stop["message"] = f"表定 {clock(scheduled)}"
        return
    next_at = departure.get("next_scheduled_at")
    if now_ts >= scheduled + departure["grace_seconds"] and next_at and next_at > now_ts:
        stop["message"] = f"下班表定 {clock(next_at)}（前班動態未確認）"
        return
    minutes = max(0, (now_ts - scheduled) // 60)
    observed = departure.get("observed_at")
    if departure["status"] == "not_departed" and observed is not None and -15 <= now_ts - observed <= FRESH_SECONDS:
        stop["message"] = f"未發車 {minutes}分" if minutes else "未發車"
    else:
        stop["message"] = f"未確認 逾{minutes}分"


@lru_cache(maxsize=1)
def _holiday_overrides() -> dict[str, bool]:
    # Realtime requests must not wait for a calendar network request.
    try:
        data = json.loads((Path(__file__).parent / "data" / "holidays.json").read_text(encoding="utf-8"))
        return {item["date"]: bool(item.get("isHoliday", True))
                for year in data.get("holidays", {}).values() for item in year}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _active(days: dict, date: datetime) -> bool:
    holiday = _holiday_overrides().get(date.date().isoformat(), date.weekday() >= 5)
    return bool((holiday and days.get("holiday") == 1) or
                days.get(("mon", "tue", "wed", "thu", "fri", "sat", "sun")[date.weekday()]) == 1)


def schedule_context(
    rows: list[dict], pathid: int, origin: str, scheduled_at: int, *,
    now_ts: int | None = None, reported_next: int | None = None,
) -> dict:
    """Find real origin times on this direction, never a downstream stop time."""
    date = datetime.fromtimestamp(scheduled_at, TAIPEI).replace(hour=0, minute=0, second=0, microsecond=0)
    departures: dict[int, str | None] = {}
    headways = []
    for row in rows:
        if row["direction"] != pathid:
            continue
        payload = row["payload"]
        for offset in (-1, 0, 1):
            service_date = date + timedelta(days=offset)
            if not _active(row["service_days"], service_date):
                continue
            if row["kind"] == "timetable":
                stops = sorted(payload.get("stop_times") or [], key=lambda stop: stop.get("seq") or 0)
                if not stops or str(stops[0].get("stopid")) != origin:
                    continue
                seconds = _clock_seconds(stops[0].get("departure"))
                if seconds is not None:
                    at = int((service_date + timedelta(seconds=seconds)).timestamp())
                    departures[at] = payload.get("trip_id")
            elif row["kind"] == "frequency":
                start, end = _clock_seconds(payload.get("start")), _clock_seconds(payload.get("end"))
                if start is None or end is None:
                    continue
                if end < start:
                    end += 86400
                since_midnight = scheduled_at - int(service_date.timestamp())
                if start <= since_midnight <= end:
                    try:
                        headway = int(payload.get("max_headway") or payload.get("min_headway") or 0)
                        if headway > 0:
                            headways.append(headway * 60)
                    except (TypeError, ValueError):
                        pass
    # Only use adjacent timetable trips if this exact origin departure matches.
    following = sorted(at for at in departures if at > scheduled_at)
    previous = sorted(at for at in departures if at < scheduled_at)
    next_at = following[0] if scheduled_at in departures and following else None
    if not headways and scheduled_at in departures:
        if next_at is not None:
            headways.append(next_at - scheduled_at)
        elif previous:
            headways.append(scheduled_at - previous[-1])
    if reported_next is not None and reported_next > max(scheduled_at, now_ts or scheduled_at):
        if not headways:
            headways.append(reported_next - scheduled_at)
        next_at = reported_next
    else:
        next_at = next((at for at in following if at > (now_ts or scheduled_at)), None) if scheduled_at in departures else None
    return {
        "trip_id": departures.get(scheduled_at),
        "grace_seconds": grace_seconds(min(headways) if headways else None),
        "next_scheduled_at": next_at,
    }


class DepartureTracker:
    """Bounded, per-route/direction memory of provider-identified departures.

    Advancing NextBusTime is not proof that the preceding trip departed. Keep
    the pending observation through its grace period, but never suppress live
    ETA or blacklist a late bus. Source age is preserved across every refresh.
    """
    def __init__(self) -> None:
        self._pending: OrderedDict[tuple[str, int], dict] = OrderedDict()
        self._lock = threading.Lock()

    def update(
        self, routeid: str, paths: dict, items: list[dict], *, now_ts: int,
        load_schedules: Callable[[], list[dict]],
    ) -> dict[int, dict]:
        result = {}
        schedules = None
        with self._lock:
            for key, value in list(self._pending.items()):
                if now_ts - value["scheduled_at"] > MAX_PENDING_SECONDS:
                    self._pending.pop(key)
            for pathid, path in paths.items():
                stops = path.get("stops") or []
                if not stops:
                    continue
                origin = str(min(stops, key=lambda stop: stop.get("seq") or 0)["stopid"])
                origin_items = [item for item in items
                                if str(item.get("StopID") or item.get("StopUID")) == origin
                                and str(item.get("Direction", 0)) == str(pathid)]
                latest = max(origin_items, key=lambda item: observed_at(item) or 0, default=None)
                key = (routeid, pathid)
                old = self._pending.get(key)
                fresh = latest is not None and (at := observed_at(latest)) is not None and -15 <= now_ts - at <= FRESH_SECONDS
                if fresh and str(latest.get("StopStatus")) in {"2", "3", "4"}:
                    self._pending.pop(key, None)
                    continue
                candidate = None
                if latest is not None and str(latest.get("StopStatus")) not in {"2", "3", "4"}:
                    scheduled = origin_departure_at(latest, now_ts)
                    # A live numeric estimate is not a pending-departure hint.
                    waiting = str(latest.get("StopStatus")) == "1"
                    if scheduled is not None and (waiting or latest.get("EstimateTime") is None) and -86400 <= now_ts - scheduled <= MAX_PENDING_SECONDS:
                        if schedules is None:
                            schedules = load_schedules()
                        candidate = {
                            "scheduled_at": scheduled,
                            "observed_at": observed_at(latest),
                            "status": "not_departed" if waiting else "unconfirmed",
                            **schedule_context(
                                schedules, pathid, origin, scheduled, now_ts=now_ts,
                                reported_next=timestamp(latest.get("NextBusTime")),
                            ),
                        }
                if old and candidate and candidate["scheduled_at"] > old["scheduled_at"]:
                    # Keep the earlier pending trip while the new one is still
                    # in the future. At its departure time the new slot takes over.
                    if now_ts < candidate["scheduled_at"]:
                        if old.get("next_scheduled_at") is None:
                            old = dict(old, grace_seconds=grace_seconds(candidate["scheduled_at"] - old["scheduled_at"]))
                        old = dict(old, next_scheduled_at=candidate["scheduled_at"], status="unconfirmed")
                        candidate = old
                elif old and candidate and candidate["scheduled_at"] == old["scheduled_at"]:
                    # A feed dropping the date after midnight must not reset us.
                    if (candidate["observed_at"] or 0) < (old["observed_at"] or 0):
                        candidate = old
                elif old and candidate and candidate["scheduled_at"] < old["scheduled_at"]:
                    if (candidate["observed_at"] or 0) < (old["observed_at"] or 0):
                        candidate = old
                if candidate is None and old is not None:
                    candidate = dict(old, status="unconfirmed")
                if candidate is not None:
                    self._pending[key] = candidate
                    self._pending.move_to_end(key)
                    result[pathid] = copy.deepcopy(candidate)
            while len(self._pending) > 4096:
                self._pending.popitem(last=False)
        return result
