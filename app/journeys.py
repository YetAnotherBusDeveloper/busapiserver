"""Fixed-date, round-based timetable routing; no realtime or database writes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import heapq
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HORIZON_SECONDS = 2 * 24 * 60 * 60
Seconds = Annotated[int, Field(strict=True, ge=0, lt=HORIZON_SECONDS)]
# Every accepted snapshot must be able to emit next-day datetime values.
ServiceDate = Annotated[date, Field(le=date(9999, 12, 30))]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stop(Model):
    id: Identifier
    name: str
    min_transfer_seconds: Annotated[int, Field(strict=True, ge=0, le=3600)] = 120


class StopTime(Model):
    stop_id: Identifier
    arrival: Seconds
    departure: Seconds
    pickup: bool = True
    dropoff: bool = True


class Trip(Model):
    id: Identifier
    route_id: Identifier
    stop_times: Annotated[tuple[StopTime, ...], Field(min_length=2)]

    @model_validator(mode="after")
    def ordered(self):
        previous = -1
        for stop in self.stop_times:
            if not previous <= stop.arrival <= stop.departure:
                raise ValueError("trip times must be monotonic; use seconds beyond 86400 after midnight")
            previous = stop.departure
        return self


class Footpath(Model):
    from_stop: Identifier
    to_stop: Identifier
    seconds: Annotated[int, Field(strict=True, gt=0, le=7200)]


class Timetable(Model):
    schema_version: Literal[1]
    dataset_id: Identifier
    source: Annotated[str, Field(min_length=1)]
    is_demo: bool
    service_date: ServiceDate
    timezone: Literal["Asia/Taipei"] = "Asia/Taipei"
    stops: Annotated[tuple[Stop, ...], Field(min_length=1)]
    trips: tuple[Trip, ...]
    footpaths: tuple[Footpath, ...] = ()

    @model_validator(mode="after")
    def references(self):
        ids = {s.id for s in self.stops}
        if len(ids) != len(self.stops) or len({t.id for t in self.trips}) != len(self.trips):
            raise ValueError("stop and trip IDs must be unique")
        if any(s.stop_id not in ids for t in self.trips for s in t.stop_times):
            raise ValueError("unknown stop in trip")
        if any(f.from_stop not in ids or f.to_stop not in ids for f in self.footpaths):
            raise ValueError("unknown stop in footpath")
        return self


class JourneyQuery(Model):
    origin: Identifier
    destination: Identifier
    service_date: ServiceDate
    departure_seconds: Seconds
    max_transfers: Annotated[int, Field(strict=True, ge=0, le=4)] = 2


class Leg(Model):
    mode: Literal["bus", "walk"]
    from_stop: str
    to_stop: str
    departure: datetime
    arrival: datetime
    trip_id: str | None = None
    route_id: str | None = None


class Journey(Model):
    departure: datetime
    arrival: datetime
    boardings: int
    transfers: int
    legs: tuple[Leg, ...]


class JourneyResult(Model):
    dataset_id: str
    source: str
    is_demo: bool
    service_date: date
    timezone: str
    status: Literal["ok", "unreachable"]
    journeys: tuple[Journey, ...]


class Router:
    """RAPTOR-style boarding rounds with directed Dijkstra walking closure.

    Each round keeps the earliest arrival for exactly k boardings at each stop.
    Every boardable trip occurrence is relaxed to ALL permitted downstream
    alightings, preserving through-rides without charging a transfer margin.
    No route-order/non-overtaking assumption is made. With stop-only margins,
    earlier arrival dominates later arrival within a round (waiting is allowed).
    The output frontier compares arrival and transfers, not boardings: walking
    and a single bus both have zero transfers. Fewer boardings break exact ties.
    """

    def __init__(self, timetable: Timetable):
        self.timetable = timetable
        self.stops = {s.id: s for s in timetable.stops}
        self.walks: dict[str, list[Footpath]] = {}
        self.boardings: dict[str, list[tuple[Trip, int]]] = {}
        for path in timetable.footpaths:
            self.walks.setdefault(path.from_stop, []).append(path)
        for trip in timetable.trips:
            for index, stop in enumerate(trip.stop_times[:-1]):
                if stop.pickup:
                    self.boardings.setdefault(stop.stop_id, []).append((trip, index))
        self.midnight = datetime.combine(timetable.service_date, datetime.min.time(), timezone(timedelta(hours=8)))

    def timestamp(self, seconds: int) -> datetime:
        return self.midnight + timedelta(seconds=seconds)

    def route(self, query: JourneyQuery) -> JourneyResult:
        if query.service_date != self.timetable.service_date:
            raise ValueError("service_date is not covered by this fixed-date timetable")
        if query.origin not in self.stops or query.destination not in self.stops:
            raise ValueError("unknown origin or destination stop")
        # Labels carry immutable paths, so later improvements cannot corrupt
        # predecessor chains already used by another label.
        labels = {query.origin: (query.departure_seconds, ())}
        candidates: dict[int, Journey] = {}
        best_arrival = HORIZON_SECONDS

        for count in range(query.max_transfers + 2):
            queue = [(value[0], stop) for stop, value in labels.items()]
            heapq.heapify(queue)
            while queue:
                arrival, stop = heapq.heappop(queue)
                if labels[stop][0] != arrival:
                    continue
                for path in self.walks.get(stop, ()):
                    end = arrival + path.seconds
                    if end >= labels.get(path.to_stop, (HORIZON_SECONDS, ()))[0]:
                        continue
                    leg = Leg(mode="walk", from_stop=stop, to_stop=path.to_stop,
                              departure=self.timestamp(arrival), arrival=self.timestamp(end))
                    labels[path.to_stop] = (end, labels[stop][1] + (leg,))
                    heapq.heappush(queue, (end, path.to_stop))

            target = labels.get(query.destination)
            if target and target[0] < best_arrival:
                best_arrival = target[0]
                transfers = max(0, count - 1)
                # Rounds 0 and 1 share the zero-transfer objective. A faster
                # direct bus replaces a slower walk; an equal-time walk wins.
                candidates[transfers] = Journey(
                    departure=self.timestamp(query.departure_seconds),
                    arrival=self.timestamp(target[0]), boardings=count,
                    transfers=transfers, legs=target[1])
            if count == query.max_transfers + 1:
                break
            next_labels = {}
            for stop, (arrival, legs) in labels.items():
                ready = arrival + (self.stops[stop].min_transfer_seconds if count else 0)
                for trip, index in self.boardings.get(stop, ()):
                    departure = trip.stop_times[index].departure
                    if departure < ready:
                        continue
                    for end in trip.stop_times[index + 1:]:
                        if not end.dropoff or end.arrival >= next_labels.get(end.stop_id, (HORIZON_SECONDS, ()))[0]:
                            continue
                        leg = Leg(mode="bus", from_stop=stop, to_stop=end.stop_id,
                                  departure=self.timestamp(departure), arrival=self.timestamp(end.arrival),
                                  trip_id=trip.id, route_id=trip.route_id)
                        next_labels[end.stop_id] = (end.arrival, legs + (leg,))
            labels = next_labels
            if not labels:
                break
        return JourneyResult(
            dataset_id=self.timetable.dataset_id, source=self.timetable.source,
            is_demo=self.timetable.is_demo, service_date=self.timetable.service_date,
            timezone=self.timetable.timezone, status="ok" if candidates else "unreachable",
            journeys=tuple(sorted(candidates.values(), key=lambda j: (j.arrival, j.transfers, j.boardings))))


def load_router(path: str | Path) -> Router:
    return Router(Timetable.model_validate_json(Path(path).read_text(encoding="utf-8")))
