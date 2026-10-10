"""Reproducible synthetic benchmark; excludes loading/index construction."""
import argparse
import statistics
import time

from app.journeys import JourneyQuery, Router, Timetable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trips", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=50)
    args = parser.parse_args()
    if args.trips < 1 or args.queries < 1 or args.trips > 10000:
        parser.error("trips must be 1..10000 and queries must be positive")
    table = Timetable.model_validate(dict(
        schema_version=1, dataset_id="benchmark", source="synthetic", is_demo=True,
        service_date="2026-10-10", stops=[dict(id=str(s), name=str(s)) for s in range(20)],
        trips=[dict(id=str(t), route_id=str(t % 10), stop_times=[
            dict(stop_id=str(s), arrival=28800 + t * 5 + s * 120,
                 departure=28800 + t * 5 + s * 120) for s in range(20)]) for t in range(args.trips)]))
    engine = Router(table)
    samples = []
    for index in range(args.queries):
        request = JourneyQuery(origin="0", destination="19", service_date=table.service_date,
                               departure_seconds=28800 + (index % min(args.trips, 120)) * 5,
                               max_transfers=2)
        start = time.perf_counter()
        result = engine.route(request)
        samples.append((time.perf_counter() - start) * 1000)
        assert result.status == "ok"
    print(f"trips={args.trips} stop_times={args.trips * 20} queries={args.queries} "
          f"median_ms={statistics.median(samples):.2f} p95_ms={sorted(samples)[int(.95 * (len(samples)-1))]:.2f}")


if __name__ == "__main__":
    main()
